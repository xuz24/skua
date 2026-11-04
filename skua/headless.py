"""Bounded unattended adapter. Policy authorizes actions; it is not isolation."""
import asyncio
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import signal
import sys
import time

from .agent import run_turn
from .client import ChatClient
from .config import resolve_credential
from .patches import recovery_dir
from .permissions import sensitive
from .run_context import RunContext
from .session import ModelIdentity, Session
from .storage import encoded
from .tools import Runtime

EXIT_CODES = {'completed': 0, 'failed': 1, 'cancelled': 130,
              'budget_exhausted': 124, 'approval_required': 3}

# Safe summaries: never interpolate task text, credentials, or exception bodies.
OUTCOMES = {
    'turn_completed': ('Agent turn completed; task success has not been independently verified.',
                       'Check the requested outputs or external verifier results.'),
    'time_limit': ('Task time limit reached.', 'Inspect partial work before retrying with an explicitly chosen time limit.'),
    'tool_limit': ('Total tool-call limit reached.', 'Inspect the transcript and partial work before continuing.'),
    'policy_review_required': ('An operation requires approval under the selected policy.',
                               'Review the denied operation in the transcript.'),
    'recovery_pending': ('Unresolved patch recovery requires review.', 'Use an interactive session to review recovery before retrying.'),
    'unresolved_tool_error': ('The turn ended with an unresolved tool or lint failure.',
                              'Inspect the last tool result and validate any partial changes.'),
    'turn_error': ('The agent turn failed.', 'Inspect the transcript for the model or tool error.'),
    'execution_error': ('The runner encountered an execution error.', 'Inspect available artifacts and partial work before retrying.'),
    'configuration_error': ('Run configuration is invalid or unavailable.', 'Check the selected profile, credentials, and configuration files.'),
    'signal': ('Run interrupted by a signal.', 'Inspect partial work before restarting.'),
    'cancelled': ('Run cancelled.', 'Inspect partial work before restarting.'),
    'input_cancelled': ('Task input cancelled.', 'Provide the task again when ready.'),
    'input_error': ('Task input could not be read.', 'Supply a nonempty UTF-8 task of at most 1 MB.'),
    'input_or_artifact_error': ('Could not read task input or prepare output artifacts.',
                                'Check task input and output paths; output files must be new and their parent directories must exist.'),
    'artifact_write_error': ('Could not finish writing output artifacts.',
                             'Check disk space and permissions; artifact files may be incomplete.'),
}


def describe(result):
    result['message'], result['next_step'] = OUTCOMES.get(result['reason'],
        ('Run stopped.', 'Inspect available artifacts before continuing.'))
    return result


def positive_seconds(value):
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('time limit must be positive and finite')
    return seconds


def report(status, reason, *, policy, managed_git, tool_calls=0, elapsed=0):
    # Deliberately omit prompt, paths, endpoint, model, credentials, and exceptions.
    return describe({'version': 1, 'status': status, 'reason': reason, 'exit_code': EXIT_CODES[status],
            'policy': policy, 'managed_git': managed_git, 'tool_calls': tool_calls,
            'elapsed_seconds': round(elapsed, 3), 'task_success_verified': False,
            'provider_usage': None, 'diagnostics': []})


class TaskKeyPermissions:
    """Trusted container adapter opt-in, never a task-configurable permission.

    Only new .key paths under explicit directories are admitted. This is a
    review policy, not a shell sandbox or a proof of command write intent.
    """
    def __init__(self, config, directories):
        self.config = config
        self.directories = tuple(Path(p).absolute() for p in directories)
        self.admitted = set()

    def allows(self, review):
        if review.action not in {'command', 'patch', 'read'}:
            return False
        paths = {Path(p) for p in (*review.targets, *review.policy_paths)}
        keys = {p for p in paths if sensitive(self.config, p)}
        if not keys:
            return False
        for path in keys:
            if (path.suffix != '.key' or sensitive(self.config, path, ignore_key_suffix=True)
                    or path != path.resolve()
                    or not any(path.is_relative_to(root) and root != path for root in self.directories)
                    or path.is_dir()
                    or (path not in self.admitted and (path.exists() or review.action == 'read'))):
                return False
        self.admitted.update(keys)
        return True


async def execute(config, prompt, *, policy, max_seconds=300, max_tool_calls=100,
                  managed_git=False, client_factory=ChatClient, handle_signals=False, task_key_directories=()):
    """Return metadata, canonical session, and credential for artifact redaction."""
    if policy not in {'review', 'disposable', 'container'}:
        raise ValueError('unknown unattended policy')
    if task_key_directories and policy != 'container':
        raise ValueError('task key permissions require container policy')
    task_keys = TaskKeyPermissions(config, task_key_directories)
    max_seconds = positive_seconds(max_seconds)
    if isinstance(max_tool_calls, bool) or not isinstance(max_tool_calls, int) or max_tool_calls < 1:
        raise ValueError('tool-call limit must be a positive integer')
    config = replace(config, managed_git=managed_git)
    started = time.monotonic()
    session = None
    credential = None
    reason = None
    status = 'failed'
    deadline_reason = None

    async def approve(review):
        blocked = {'sensitive'} if policy == 'container' else {'sensitive', 'outside_root'}
        blocked_reasons = set(review.reasons) & blocked
        if (blocked_reasons == {'sensitive'} and task_key_directories
                and task_keys.allows(review)):
            blocked_reasons.clear()
        if policy == 'review' or blocked_reasons:
            return 'denied'
        if policy == 'container' and review.action == 'read':
            return 'approved'
        if review.action in {'patch', 'command'}:
            return 'approved'
        if managed_git and review.action in {'checkpoint', 'commit'}:
            return 'approved'
        return 'denied'

    context = RunContext(config, started + max_seconds, max_seconds, max_tool_calls)
    runtime = Runtime(config, approve=approve, review_source='unattended_policy', run_context=context)

    async def work():
        nonlocal session, credential, status, reason
        # Never auto-restore or ignore unresolved recovery, even in disposable mode.
        directory = recovery_dir(config.root)
        if any(directory.glob('patch-*.json')):
            status, reason = 'approval_required', 'recovery_pending'
            return
        profile = config.profile()
        credential = resolve_credential(config)
        session = Session(config.root, ModelIdentity(config.selected_profile, profile.base_url, profile.model))
        session.notice('unattended_policy', f'Policy: {policy}; managed Git: {managed_git}. Not a sandbox.')
        if task_key_directories:
            session.notice('task_key_permissions', 'New task certificate keys allowed under: ' +
                           ', '.join(str(p) for p in task_keys.directories))
        async with client_factory(profile, credential) as client:
            runtime.client = client
            await run_turn(session, client, prompt, runtime=runtime, max_tool_calls=max_tool_calls)
        end = next((e for e in reversed(session.events) if e['type'] == 'turn_end'), None)
        results = [e for e in session.events if e['type'] == 'tool_result']
        if end and end['reason'] == 'tool_budget':
            status, reason = 'budget_exhausted', 'tool_limit'
        elif end and end['reason'] == 'approval_required':
            status, reason = 'approval_required', 'policy_review_required'
        elif end and end['status'] == 'cancelled':
            status, reason = 'cancelled', 'cancelled'
        elif not end or end['status'] != 'complete':
            status, reason = 'failed', 'turn_error'
        elif runtime.workflows.pending or (results and results[-1]['status'] in {'error', 'timeout', 'not_run'}):
            status, reason = 'failed', 'unresolved_tool_error'
        else:
            status, reason = 'completed', 'turn_completed'

    task = asyncio.create_task(work())
    loop = asyncio.get_running_loop()
    def stop(why):
        nonlocal deadline_reason
        if not task.done() and deadline_reason is None:
            deadline_reason = why
            task.cancel()
    timer = loop.call_later(max(0, context.deadline - time.monotonic()), stop, 'time_limit')
    previous = {}
    try:
        if handle_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.getsignal(sig)
                loop.add_signal_handler(sig, stop, 'signal')
        try:
            await task
        except asyncio.CancelledError:
            status, reason = 'cancelled', 'cancelled'
        except Exception:
            # Exceptions may contain endpoint bodies, shell text, or credentials.
            status, reason = 'failed', 'execution_error'
    finally:
        timer.cancel()
        for sig, handler in previous.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)
        if session is not None:
            session.close()
    if deadline_reason:
        status, reason = ('budget_exhausted', 'time_limit') if deadline_reason == 'time_limit' else ('cancelled', 'signal')
    count = sum(e['type'] == 'tool_result' and e['status'] != 'not_run'
                for e in session.events) if session else 0
    result = report(status, reason, policy=policy, managed_git=managed_git, tool_calls=count,
                    elapsed=time.monotonic() - started)
    # Provider evidence only: ordinary prose is not a reliable refusal classifier.
    result['diagnostics'] = ['model_refusal'] if session and any(
        e['type'] == 'notice' and e['code'] == 'model_refusal' for e in session.events) else []
    return result, session, credential


def transcript_bytes(session, credential):
    content = encoded(session, None)
    if not credential:
        return content
    def redact(value):
        if isinstance(value, str):
            return value.replace(credential, '[REDACTED]')
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, dict):
            return {redact(key): redact(item) for key, item in value.items()}
        return value
    # Redact JSON strings, never raw JSON syntax (a short/numeric key could corrupt it).
    return b''.join((json.dumps(redact(json.loads(line)), ensure_ascii=True) + '\n').encode()
                    for line in content.splitlines())


def cli_run(config, args, *, task_key_directories=()):
    """Read task bytes without putting task text or credentials on the command line."""
    files = {}
    result = report('failed', 'input_error', policy=args.policy, managed_git=args.managed_git == 'on')
    try:
        # Exclusive, private output files; never overwrite a project file or follow a symlink.
        for name in ('result_file', 'transcript_file'):
            path = getattr(args, name)
            if path is not None:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                files[name] = os.fdopen(fd, 'wb')
        if args.prompt_file == '-':
            if sys.stdin.isatty():
                raise ValueError('stdin requires a pipe ending at EOF')
            prompt = sys.stdin.read(1_000_001)
        else:
            with Path(args.prompt_file).open(encoding='utf-8') as source:
                prompt = source.read(1_000_001)
        if not prompt.strip() or len(prompt.encode('utf-8')) > 1_000_000:
            raise ValueError('task must be nonempty and at most 1 MB')
        result, session, credential = asyncio.run(execute(config, prompt, policy=args.policy,
            max_seconds=args.max_seconds, max_tool_calls=args.max_tool_calls,
            managed_git=args.managed_git == 'on', handle_signals=True,
            task_key_directories=task_key_directories))
        if 'transcript_file' in files and session is not None:
            files['transcript_file'].write(transcript_bytes(session, credential))
    except (KeyboardInterrupt, EOFError):
        result = report('cancelled', 'input_cancelled', policy=args.policy, managed_git=args.managed_git == 'on')
    except Exception:
        result = report('failed', 'input_or_artifact_error', policy=args.policy, managed_git=args.managed_git == 'on')
    try:
        if 'result_file' in files:
            files['result_file'].write((json.dumps(result) + '\n').encode())
        for output in files.values():
            output.flush()
    except OSError:
        result['status'], result['reason'], result['exit_code'] = 'failed', 'artifact_write_error', 1
    finally:
        for output in files.values():
            try:
                output.close()
            except OSError:
                result['status'], result['reason'], result['exit_code'] = 'failed', 'artifact_write_error', 1
    print(json.dumps(describe(result)))
    return result['exit_code']
