"""Validated sequential read/command tools and per-operation authorization."""
import asyncio
import json
from dataclasses import dataclass, replace
import os
from pathlib import Path
import shlex
import stat

from .config import ConfigError
from .discovery import discover
from .environment import prepare_command
from .execution import run_command, _finish_despite_cancellation
from .permissions import AccessError, Review, Target, deny_review, lexical, sensitive
from .patches import validate_changes


def schema(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': required,
                           'additionalProperties': False}}}


PATH = {'type': 'string', 'description': 'Absolute or project-relative path.'}
CATALOG = [
    schema('apply_patch', 'Create, replace, delete or rename UTF-8 text files after full diff review. '
           'Git-backed patches include a separate existing-work checkpoint when needed and one managed commit after approved lint. '
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
           'Retained output is inspectable without rerunning.',
           {'command': {'type': 'string', 'minLength': 1}}, ['command']),
]


def validate(name, arguments):
    definition = next((s['function']['parameters'] for s in CATALOG if s['function']['name'] == name), None)
    if definition is None or not isinstance(arguments, dict):
        return 'Unknown tool or invalid argument object.'
    if set(arguments) - definition['properties'].keys() or set(definition['required']) - arguments.keys():
        return 'Missing required arguments or unknown argument names.'
    for key, value in arguments.items():
        rule = definition['properties'][key]
        if rule['type'] == 'array':
            error = validate_changes(value)
            if error:
                return error
        elif rule['type'] == 'string':
            if not isinstance(value, str) or not value or '\0' in value or len(value) > 100_000:
                return f'{key} must be a nonempty string without NUL, at most 100000 characters.'
        elif type(value) is not int or not rule.get('minimum', 1) <= value <= rule.get('maximum', 1_000_000_000):
            return f'{key} is outside its integer bounds.'
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
    def __init__(self, config, *, approve=deny_review, continue_block=None, on_notice=lambda _: None):
        self.config = config
        self.approve = approve
        self.continue_block = continue_block
        self.on_notice = on_notice
        self.instructions = None
        from .workflows import Workflows
        self.workflows = Workflows(self)

    async def review(self, session, call_seq, review):
        if len(json.dumps(review.summary, ensure_ascii=False).encode('utf-8')) > 14_000_000:
            raise AccessError('Review exceeds the session record budget; split the change into smaller patches.')
        try:
            decision = await self.approve(review)
            if decision not in {'approved', 'denied', 'cancelled'}:
                decision = 'denied'
        except (asyncio.CancelledError, EOFError):
            decision = 'cancelled'
        session._append('approval', turn=session.active_turn, call_seq=call_seq,
                        action=review.action, decision=decision, reasons=list(review.reasons),
                        targets=list(review.targets), summary=review.summary, review_sha256=review.review_sha256)
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
        error = validate(name, args)
        if error:
            return Result('error', error, code='invalid_arguments')
        try:
            if name == 'apply_patch':
                return await self.workflows.patch(session, call_seq, args)
            if name == 'run_command':
                return await self.command(session, call_seq, args['command'])
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
                return Result('ok', bounded('\n'.join(str(p) for p in paths), self.config.output.model_max_bytes),
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
            return Result('ok', bounded('\n'.join(matches), self.config.output.model_max_bytes),
                          {'matches': len(matches), 'skipped_nontext_or_large': skipped, 'truncated': truncated})
        except (AccessError, ConfigError, OSError, RuntimeError):
            return Result('error', 'Tool failed: path unavailable, changed since review, or unsupported. '
                          'No contents returned.', code='access_error')

    async def command(self, session, call_seq, command):
        config = self.config
        invocation = prepare_command(config, command)
        reasons = {'execute'}
        targets = []
        evidence = []
        hidden = []
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            tokens = [command]
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
        summary = (f'Execute in {config.root} using {invocation.shell}:\n{command}\n'
                   f'Timeout: {config.commands.timeout_seconds} seconds.\n'
                   f'Shell startup: {"enabled" if config.shell.startup_enabled else "disabled"}; '
                   f'environment: {config.environment.kind} '
                   f'{config.environment.path or config.environment.name or ""}. '
                   'Startup/activation scripts may run.\n'
                   'Command output is sent to the model. This approval permits command side effects. '
                   'Path inspection is best-effort, not a filesystem sandbox.')
        if 'sensitive' in reasons:
            summary += '\nDetected sensitive paths: approval also permits exposing their contents to the model.'
        if hidden:
            summary += '\nDot paths: ' + ', '.join(hidden)
        review = Review.create('command', sorted(reasons), sorted(set(targets)), summary,
                               [invocation.argv, evidence])
        decision = await self.review(session, call_seq, review)
        if decision != 'approved':
            return Result(decision, 'Command ' + decision + '. Do not retry it.')
        if prepare_command(config, command) != invocation or any(str(Path(p).resolve()) != r for p, r in evidence):
            raise AccessError('Command environment or paths changed since review.')
        before, before_partial = await asyncio.to_thread(file_snapshot, config.root)
        baseline = await self.workflows.command_before()
        if any(str(Path(p).resolve()) != r for p, r in evidence):
            raise AccessError('Command paths changed since review.')
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
        summary += '\nChanges remain uncommitted. Metadata observation excludes .git and external paths; '
        summary += 'it is not a complete audit or rollback.'
        if before_partial or after_partial or len(changes) > 200:
            summary += ' Change listing is incomplete.'
        self.on_notice(summary + f'\nRetained output: /output {call_seq}')
        status = result.status if result.status != 'completed' else ('ok' if result.returncode == 0 else 'error')
        committed = None
        if status != 'cancelled' and not (before_partial or after_partial):
            try:
                committed = await self.workflows.command_commit(session, call_seq, baseline, changes)
            except (AccessError, OSError, UnicodeError) as exc:
                self.on_notice('Command changes remain available; commit was not completed: ' +
                               (str(exc) if isinstance(exc, AccessError) else 'unsupported file or filesystem error'))
        return Result(status, f'Command {status}; exit code {result.returncode}.\n{result.preview()}\n{summary}',
                      {'returncode': result.returncode, 'changes': changes[:200],
                       'changes_incomplete': before_partial or after_partial or len(changes) > 200,
                       'commit': committed},
                      output_record(result, config), 'command_failed' if status == 'error' else None)
