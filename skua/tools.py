"""Validated sequential read/command tools and per-operation authorization."""
import asyncio
import json
import math
import time
from dataclasses import dataclass, replace
import os
import re
from pathlib import Path
import shlex
import stat

from .activity import Activity
from .config import ConfigError
from .discovery import discover
from .environment import prepare_command
from .execution import run_command, _finish_despite_cancellation
from .permissions import AccessError, Review, Target, deny_review, lexical, sensitive
from .patches import validate_changes


def command_tokens(command):
    """Heuristic path candidates, not a shell parser or security boundary.

    Python heredoc bodies are stdin data. Scan their quoted literals for paths without
    treating embedded-language punctuation (e.g. Python division) as shell words.
    Unclosed/complex heredocs retain the conservative whole-command fallback.
    """
    lines = command.splitlines(keepends=True)
    shell, literals = [], []
    index = 0
    while index < len(lines):
        line = lines[index]
        shell.append(line)
        index += 1
        markers = re.findall(r"(?<!<)<<(-?)\s*(?:'([A-Za-z_][\w]*)'|\"([A-Za-z_][\w]*)\"|([A-Za-z_][\w]*))", line)
        for tabs, single, double, bare in markers:
            delimiter = single or double or bare
            body = []
            while index < len(lines):
                candidate = lines[index].rstrip('\r\n')
                index += 1
                if (candidate.lstrip('\t') if tabs else candidate) == delimiter:
                    break
                body.append(candidate)
            else:
                shell, literals = [command], []
                break
            text = '\n'.join(body)
            if re.search(r'(?:^|\s|[;&|])(?:[^\s]*/)?python(?:\d+(?:\.\d+)*)?\s', line):
                literals.extend(a or b for a, b in re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"", text))
            else:
                shell.append(text + '\n' + delimiter + '\n')
        if shell == [command]:
            break
    try:
        lexer = shlex.shlex(''.join(shell), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer) + literals
    except ValueError:
        return [command]


def schema(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': required,
                           'additionalProperties': False}}}


PATH = {'type': 'string', 'description': 'Absolute or project-relative path.'}
CATALOG = [
    schema('apply_patch', 'Create, replace, delete or rename UTF-8 text files after full diff review. '
           'When git.managed is enabled, Git-backed patches include an existing-work checkpoint when needed and a reviewed commit after configured lint. Otherwise edits remain uncommitted. '
           'For notes use the configured notes directory unless the user chooses another path.',
           {'message': {'type': 'string'}, 'changes': {'type': 'array', 'minItems': 1, 'maxItems': 100,
            'items': {'type': 'object', 'properties': {'op': {'type': 'string', 'enum': ['write', 'delete', 'rename']},
                      'path': PATH, 'destination': PATH, 'content': {'type': 'string'}},
                      'required': ['op', 'path'], 'additionalProperties': False}}}, ['message', 'changes']),
    schema('list_files', 'List files recursively, respecting .gitignore; directory symlinks are not followed.',
           {'path': PATH}, ['path']),
    schema('read_file', 'Read UTF-8 text (at most 1 MB); explicit files may be ignored. Lines are 1-based.',
           {'path': PATH, 'start_line': {'type': 'integer', 'minimum': 1},
            'max_lines': {'type': 'integer', 'minimum': 1, 'maximum': 2000}}, ['path']),
    schema('search_files', 'Case-sensitive literal text search; directory discovery respects .gitignore. '
           'An explicit file bypasses ignore filtering. Sensitive contents require approval.',
           {'path': PATH, 'query': {'type': 'string', 'minLength': 1}}, ['path', 'query']),
    schema('run_command', 'Run a shell command after approval. May modify files; not a sandbox. '
           'Optional timeout_seconds (alias timeout) is a positive number of seconds. '
           'The effective limit is the smallest of the requested timeout, configured command ceiling, '
           'and remaining unattended task time. Omit it to use the configured ceiling. '
           'Output limits are configured in bytes, not per-call arguments. '
           'Retained output is inspectable without rerunning.',
           {'command': {'type': 'string', 'minLength': 1},
            'timeout_seconds': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1e12},
            'timeout': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1e12,
                        'description': 'Alias for timeout_seconds; do not supply conflicting values.'}}, ['command']),
]


def validate(name, arguments):
    definition = next((s['function']['parameters'] for s in CATALOG if s['function']['name'] == name), None)
    if definition is None:
        return 'Tool did not execute: unknown tool. Use a tool from the supplied catalog.'
    if not isinstance(arguments, dict):
        return f'{name} did not execute: arguments must be a JSON object.'
    unknown = set(arguments) - definition['properties'].keys()
    missing = set(definition['required']) - arguments.keys()
    if unknown or missing:
        details = [f'{name} did not execute.']
        if unknown:
            details.append('Unsupported arguments: ' + ', '.join(sorted(map(str, unknown))) + '.')
        if missing:
            details.append('Missing required arguments: ' + ', '.join(sorted(missing)) + '.')
        details.append('Supported arguments: ' + ', '.join(definition['properties']) + '.')
        corrected = {key: value for key, value in arguments.items() if key in definition['properties']}
        if not missing and validate(name, corrected) is None:
            details.append('Retry with: ' + json.dumps(corrected, ensure_ascii=False))
        else:
            details.append('Supply the required fields with valid values and remove unsupported fields before retrying.')
        return ' '.join(details)
    for key, value in arguments.items():
        rule = definition['properties'][key]
        if rule['type'] == 'array':
            error = validate_changes(value)
            if error:
                return error
        elif rule['type'] == 'string':
            if not isinstance(value, str) or not value or '\0' in value or len(value) > 100_000:
                return f'{key} must be a nonempty string without NUL, at most 100000 characters.'
        elif rule['type'] == 'number':
            if type(value) not in (int, float) or not 0 < value <= 1e12 or not math.isfinite(value):
                return f'{key} must be a finite positive number of seconds (at most 1e12), not a string or boolean.'
        elif type(value) is not int or not rule.get('minimum', 1) <= value <= rule.get('maximum', 1_000_000_000):
            return f'{key} is outside its integer bounds.'
    if name == 'run_command' and 'timeout' in arguments and 'timeout_seconds' in arguments:
        if arguments['timeout'] != arguments['timeout_seconds']:
            return 'Supply timeout_seconds or timeout, or give both the same value.'
    return None


@dataclass
class Result:
    status: str
    text: str
    data: dict | None = None
    output: dict | None = None
    code: str | None = None

    def record(self, session, call_seq):
        session._append('tool_result', turn=session.active_turn, call_seq=call_seq,
                        status=self.status, model_text=self.text, output=self.output,
                        data=self.data or {}, error=({'code': self.code, 'message': self.text,
                                                    'retryable': False} if self.code else None))


def bounded(text, limit):
    raw = text.encode()
    if len(raw) <= limit:
        return text
    return raw[:limit].decode('utf-8', errors='ignore') + '\n[Output truncated; request a narrower range.]'


def output_record(result, config):
    chunks = []
    observed = retained = 0
    for stream, capture in [('stdout', result.stdout), ('stderr', result.stderr)]:
        observed += capture.total_bytes
        retained += len(capture.head) + len(capture.tail)
        chunks.append({'stream': stream, 'text': capture.head.decode('utf-8', errors='replace')})
        if capture.omitted_bytes:
            chunks.append({'omitted_bytes': capture.omitted_bytes})
        chunks.append({'stream': stream, 'text': capture.tail.decode('utf-8', errors='replace')})
    return dict(chunks=chunks, observed_bytes=observed, retained_bytes=retained,
                model_max_bytes=config.output.model_max_bytes, retain_max_bytes=config.output.retain_max_bytes,
                capture_truncated=observed > retained,
                model_truncated=(observed > retained or
                                 result.stdout.total_bytes > (config.output.model_max_bytes + 1) // 2 or
                                 result.stderr.total_bytes > config.output.model_max_bytes // 2))


def file_snapshot(root):
    """Metadata only, including ignored files; never traverse symlinks or .git."""
    entries = {}
    incomplete = False
    def failed(_):
        nonlocal incomplete
        incomplete = True
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        dirs[:] = sorted(d for d in dirs if d != '.git')
        for name in sorted(files + dirs):
            path = Path(directory) / name
            try:
                s = path.lstat()
                entries[str(path.relative_to(root))] = (s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            except OSError:
                incomplete = True
            if len(entries) >= 20_000:
                return entries, True
    return entries, incomplete


class Runtime:
    def __init__(self, config, *, approve=deny_review, continue_block=None, on_notice=lambda _: None, on_activity=lambda _: None, review_source='review', run_context=None):
        self.run_context = run_context
        self.review_source = review_source
        self.config = config
        self.approve = approve
        self.continue_block = continue_block
        self.on_notice = on_notice
        self.on_activity = on_activity
        self.activity = None
        self.instructions = None
        from .workflows import Workflows
        self.workflows = Workflows(self)

    async def review(self, session, call_seq, review):
        if len(json.dumps(review.summary, ensure_ascii=False).encode('utf-8')) > 14_000_000:
            raise AccessError('Review exceeds the session record budget; split the change into smaller patches.')
        grant = session.permissions.match(self.config, review)
        if grant is not None:
            decision = 'approved'
            self.on_notice(f'Session grant {grant.id}: {review.action} authorized.')
        else:
            previous = self.activity
            self.on_activity(Activity('approval', review.action, call_seq))
            try:
                decision = await self.approve(review)
                if decision not in {'approved', 'denied', 'cancelled'}:
                    decision = 'denied'
            except (asyncio.CancelledError, EOFError):
                decision = 'cancelled'
            finally:
                self.on_activity(previous)
        session._append('approval', turn=session.active_turn, call_seq=call_seq,
                        action=review.action, decision=decision, reasons=list(review.reasons),
                        targets=list(review.targets), summary=review.summary, review_sha256=review.review_sha256,
                        authorization={'source': 'session_grant', 'grant_id': grant.id} if grant else
                                      {'source': self.review_source, 'grant_id': None})
        return decision

    async def authorize(self, session, call_seq, targets, *, contents):
        reasons = set()
        for t in targets:
            if not t.resolved.is_relative_to(self.config.root):
                reasons.add('outside_root')
            if contents and sensitive(self.config, t.path):
                reasons.add('sensitive')
        if reasons:
            review = Review.create('read', sorted(reasons), [str(t.resolved) for t in targets],
                                   'Read contents and send them to the configured model endpoint.' if contents else
                                   'List paths and send them to the configured model endpoint.',
                                   [(str(t.path), t.identity) for t in targets])
            decision = await self.review(session, call_seq, review)
            if decision != 'approved':
                return Result(decision, 'Access ' + decision + '. Do not retry this operation.')
        for target in targets:
            target.check()
        return None

    async def execute(self, session, call_seq, name, args):
        activity = Activity('tool', name, call_seq, arguments=args if isinstance(args, dict) else None)
        self.activity = activity
        self.on_activity(activity)
        result = None
        status = 'error'
        try:
            result = await self._execute(session, call_seq, name, args)
            if self.run_context:
                budget = self.run_context.budget()
                result = replace(result, data={**(result.data or {}), 'task_budget': budget},
                                 text=result.text + f'\nTask time remaining: {budget["remaining_seconds"]:.3f}s. '
                                 'The shared task deadline still applies.')
            status = result.status
            return result
        except asyncio.CancelledError:
            status = 'cancelled'
            raise
        finally:
            summary = ''
            if result:
                if result.data and 'returncode' in result.data:
                    summary = f"exit code {result.data['returncode']}"
                elif result.status != 'ok':
                    summary = result.text.splitlines()[0][:240] if result.text else ''
                elif result.data and 'count' in result.data:
                    summary = f"{result.data['count']} files"
                elif result.data and 'matches' in result.data:
                    summary = f"{result.data['matches']} matches; {result.data['skipped_nontext_or_large']} files skipped"
                elif name == 'apply_patch':
                    summary = result.text.splitlines()[0][:240]
                if result.output:
                    summary += f' · /output {call_seq}'
            self.activity = None
            self.on_activity(replace(activity, status=status, summary=summary.strip(' ·'),
                                     result_text=result.text if result else '',
                                     output=result.output if result else None,
                                     data=result.data if result else None))

    async def _execute(self, session, call_seq, name, args):
        error = validate(name, args)
        if error:
            if 'did not execute' not in error:
                error = f'{name} did not execute. {error}'
            return Result('error', error, code='invalid_arguments')
        try:
            if name == 'apply_patch':
                return await self.workflows.patch(session, call_seq, args)
            if name == 'run_command':
                return await self.command(session, call_seq, args['command'],
                                          args.get('timeout_seconds', args.get('timeout')))
            target = Target.inspect(lexical(self.config.root, args['path']))
            if name != 'search_files' or stat.S_ISDIR(target.identity[2]):
                denied = await self.authorize(session, call_seq, [target], contents=name != 'list_files')
                if denied:
                    return denied
            if name == 'read_file':
                text = await asyncio.to_thread(target.read)
                lines = text.splitlines(keepends=True)
                start = args.get('start_line', 1)
                stop = start - 1 + args.get('max_lines', 200)
                selected = ''.join(f'{i + start}: {line}' for i, line in enumerate(lines[start-1:stop]))
                if not selected:
                    selected = 'File is empty.' if not lines else f'No lines at or after {start}; total lines: {len(lines)}.'
                return Result('ok', bounded(selected, self.config.output.model_max_bytes),
                              {'path': str(target.resolved), 'total_lines': len(lines), 'start_line': start})
            if stat.S_ISDIR(target.identity[2]):
                # Discover from project root to retain ancestor ignore rules for subdirectories.
                base = self.config.root if target.resolved.is_relative_to(self.config.root) else target.resolved
                paths = await asyncio.to_thread(discover, base, start=target.resolved)
                paths = [target.path / p.relative_to(target.resolved)
                         for p in paths if p.is_relative_to(target.resolved)]
                target.check()
            else:
                paths = [target.path]
            if name == 'list_files':
                return Result('ok', bounded('\n'.join(str(p) for p in paths) or 'No files found.', self.config.output.model_max_bytes),
                              {'count': len(paths)})
            targets = []
            skipped = 0
            for path in paths:
                item = Target.inspect(path)
                if stat.S_ISREG(item.identity[2]):
                    targets.append(item)
            denied = await self.authorize(session, call_seq, targets, contents=True)
            if denied:
                return denied
            matches = []
            size = 0
            truncated = False
            for item in targets:
                try:
                    text = await asyncio.to_thread(item.read)
                except AccessError as exc:
                    if 'changed' in str(exc):
                        raise
                    skipped += 1
                    continue
                for line_number, line in enumerate(text.splitlines(), 1):
                    if args['query'] in line:
                        entry = f'{item.path}:{line_number}: {line}'
                        matches.append(entry)
                        size += len(entry.encode())
                        if size >= self.config.output.model_max_bytes:
                            truncated = True
                            break
                if truncated:
                    break
            text = '\n'.join(matches) or 'No matches found.'
            if skipped:
                text += f'\nSkipped {skipped} nontext, oversized, or unreadable files.'
            return Result('ok', bounded(text, self.config.output.model_max_bytes),
                          {'matches': len(matches), 'skipped_nontext_or_large': skipped, 'truncated': truncated})
        except (AccessError, ConfigError, OSError, RuntimeError):
            return Result('error', 'Tool could not complete: path or command environment is unavailable, '
                          'changed since review, or unsupported. Inspect the target and configuration before retrying; '
                          'any earlier changes may remain.', code='access_error')

    async def command(self, session, call_seq, command, requested_timeout=None):
        config = self.config
        ceiling = config.commands.timeout_seconds
        if requested_timeout is not None:
            ceiling = min(ceiling, requested_timeout)
        config = replace(config, commands=replace(config.commands, timeout_seconds=ceiling))
        invocation = prepare_command(config, command)
        reasons = {'execute'}
        targets = []
        evidence = []
        hidden = []
        tokens = command_tokens(command)
        for token in tokens:
            value = token.split('=', 1)[-1] if '=' in token else token
            path = lexical(config.root, value)
            if '/' in value or value.startswith('.') or path.exists() or sensitive(config, path):
                resolved = path.resolve()
                targets.append(str(resolved))
                evidence.append((str(path), str(resolved)))
                if sensitive(config, path):
                    reasons.add('sensitive')
                if not resolved.is_relative_to(config.root):
                    reasons.add('outside_root')
                if any(p.startswith('.') and p not in {'.', '..'} for p in Path(value).parts):
                    hidden.append(value)
        summary = (f'Run in {config.root} ({invocation.shell}):\n{command}\n'
                   f'Timeout: {config.commands.timeout_seconds}s · '
                   f'startup: {"on" if config.shell.startup_enabled else "off"} · '
                   f'environment: {config.environment.kind} '
                   f'{config.environment.path or config.environment.name or ""}\n'
                   'May run startup/activation scripts and modify files outside this project. '
                   'Output is sent to the model.')
        if 'sensitive' in reasons:
            summary += '\nDetected sensitive paths: approval also permits exposing their contents to the model.'
        if hidden:
            summary += '\nDot paths: ' + ', '.join(hidden)
        review = Review.create('command', sorted(reasons), sorted(set(targets)), summary,
                               [invocation.argv, evidence], policy_paths=[p for p, _ in evidence],
                               command=command, invocation=invocation)
        decision = await self.review(session, call_seq, review)
        if decision != 'approved':
            return Result(decision, 'Command ' + decision + '. Do not retry it.')
        if prepare_command(config, command) != invocation or any(str(Path(p).resolve()) != r for p, r in evidence):
            raise AccessError('Command environment or paths changed since review.')
        before, before_partial = await asyncio.to_thread(file_snapshot, config.root)
        baseline = await self.workflows.command_before()
        if any(str(Path(p).resolve()) != r for p, r in evidence):
            raise AccessError('Command paths changed since review.')
        budget = self.run_context.budget() if self.run_context else None
        effective_timeout = ceiling
        if self.run_context:
            effective_timeout = min(ceiling, self.run_context.deadline - time.monotonic())
            if effective_timeout <= 0:
                return Result('timeout', 'Task deadline reached; command did not execute.',
                              {'requested_timeout_seconds': requested_timeout, 'timeout_seconds': 0})
        if budget:
            budget.update(requested_timeout_seconds=requested_timeout,
                          effective_command_ceiling_seconds=min(budget['remaining_seconds'], round(effective_timeout, 3)))
            session.notice('command_budget', json.dumps({'call_seq': call_seq, **budget}))
        config = replace(config, commands=replace(config.commands, timeout_seconds=effective_timeout))
        result = await run_command(config, command, _invocation=invocation)
        observation = asyncio.create_task(asyncio.to_thread(file_snapshot, config.root))
        try:
            after, after_partial = await asyncio.shield(observation)
        except asyncio.CancelledError:
            after, after_partial = await _finish_despite_cancellation(observation)
            result = replace(result, status='cancelled')
        changes = [{'path': p, 'change': 'added' if p not in before else 'removed' if p not in after else 'modified'}
                   for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]
        summary = '\n'.join(f'{x["change"]}: {x["path"]}' for x in changes[:200]) or 'No project metadata changes detected.'
        summary += '\nObservation covers project file metadata only; excludes .git and external paths.'
        if before_partial or after_partial or len(changes) > 200:
            summary += ' Change listing is incomplete.'
        status = result.status if result.status != 'completed' else ('ok' if result.returncode == 0 else 'error')
        committed = None
        if status != 'cancelled' and not (before_partial or after_partial):
            try:
                committed = await self.workflows.command_commit(session, call_seq, baseline, changes)
            except (AccessError, OSError, UnicodeError) as exc:
                self.on_notice('Command changes remain available; commit was not completed: ' +
                               (str(exc) if isinstance(exc, AccessError) else 'unsupported file or filesystem error'))
        outcome = {'ok': 'Command succeeded', 'error': 'Command failed',
                   'timeout': f'Command timed out (limit {config.commands.timeout_seconds:g}s)',
                   'cancelled': 'Command cancelled'}.get(status, f'Command {status}')
        if committed and committed.get('commit'):
            summary += f'\nReviewed targets committed: {committed["commit"]}.'
        elif changes:
            summary += '\nSkua did not commit these changes.'
        if status in {'timeout', 'cancelled'}:
            summary += '\nThe command may have made partial changes; inspect them before retrying.'
        return Result(status, f'{outcome}; exit code {result.returncode}; elapsed {result.duration_seconds:.2f}s.\n'
                      f'{result.preview()}\n{summary}',
                      {'returncode': result.returncode, 'changes': changes[:200],
                       'duration_seconds': round(result.duration_seconds, 3),
                       'timeout_seconds': config.commands.timeout_seconds,
                       'requested_timeout_seconds': requested_timeout,
                       'configured_timeout_seconds': self.config.commands.timeout_seconds,
                       **({'task_budget_at_dispatch': budget} if budget else {}),
                       'changes_incomplete': before_partial or after_partial or len(changes) > 200,
                       'commit': committed},
                      output_record(result, config), 'command_failed' if status == 'error' else None)
