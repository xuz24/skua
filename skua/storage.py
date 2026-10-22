"""Validated JSONL persistence, atomic explicit writes, and stable session locks."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import fcntl
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import UUID

from .config import _valid_url, PROFILE_NAME
from .session import ModelIdentity, Session, timestamp

MAX_FILE_BYTES = 64_000_000
MAX_LINE_BYTES = 16_000_000


class StorageError(Exception):
    """Safe, content-free storage diagnostics."""


class Conflict(StorageError):
    pass


class SessionLock:
    def __init__(self, root: Path, key: str):
        self.fd = None
        path = root / '.skua/locks' / (hashlib.sha256(key.encode()).hexdigest() + '.lock')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.set_inheritable(self.fd, False)
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError):
            self.close()
            raise StorageError('Session or destination is locked or its lock cannot be acquired.') from None

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def path_for(root: Path, path: str | Path) -> Path:
    try:
        path = Path(path).expanduser()
        return (path if path.is_absolute() else root / path).resolve()
    except (OSError, ValueError, RuntimeError):
        raise StorageError('Could not resolve destination path.') from None


def snapshot(path: Path) -> bytes | None:
    try:
        # Nonblocking open prevents a named pipe from hanging a bounded reader.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise StorageError('Expected a regular file.')
            data = stream.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise StorageError('File exceeds the 64 MB session limit.')
        return data
    except FileNotFoundError:
        return None
    except OSError:
        raise StorageError('Could not read destination file.') from None


def _parse(data: bytes, line: int):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError()
            obj[key] = value
        return obj
    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError()
        return result
    def constant(_):
        raise ValueError()
    try:
        value = json.loads(data.decode('utf-8'), object_pairs_hook=pairs,
                           parse_float=number, parse_constant=constant)
        _json_values(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise StorageError(f'line {line}: invalid UTF-8 JSON, duplicate key, or non-finite value.') from None


def _json_values(value, depth=0):
    if depth > 64:
        raise ValueError('JSON nesting limit exceeded')
    if isinstance(value, str):
        value.encode('utf-8')
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError('JSON object key must be text')
            key.encode('utf-8')
            _json_values(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json_values(item, depth + 1)
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError('non-finite number')
    elif value is not None and type(value) not in {bool, int}:
        raise ValueError('unsupported JSON value')


PAYLOADS = {
    'message': {'role', 'text', 'completion', 'turn', 'call_seqs'},
    'tool_call': {'turn', 'assistant_seq', 'name', 'arguments'},
    'approval': {'turn', 'call_seq', 'action', 'decision', 'reasons', 'targets', 'summary', 'review_sha256'},
    'tool_result': {'turn', 'call_seq', 'status', 'model_text', 'output', 'data', 'error'},
    'model_changed': {'previous', 'current', 'reason', 'confirmed'},
    'turn_end': {'turn', 'status', 'reason'},
    'notice': {'turn', 'code', 'text'},
}
HEADER = {'type', 'schema_version', 'session_id', 'project_root', 'created_at', 'saved_at', 'name', 'initial_model', 'event_count'}


def validate(header: dict, events: list[dict], root: Path):
    line = 1
    def check(condition, field):
        if not condition:
            raise StorageError(f'line {line}: {field}: invalid or inconsistent field.')
    def fields(value, expected, field):
        check(isinstance(value, dict) and set(value) == expected, field)
    def text(value, field, nonempty=False):
        check(isinstance(value, str) and (not nonempty or bool(value.strip())), field)
        try:
            value.encode('utf-8')
        except UnicodeError:
            check(False, field)
    def integer(value, field, minimum=0):
        check(type(value) is int and value >= minimum, field)
    def enum(value, values, field):
        check(isinstance(value, str) and value in values, field)
    def date(value, field):
        text(value, field, True)
        try:
            check(value.endswith('Z') and datetime.fromisoformat(value).utcoffset().total_seconds() == 0, field)
        except (ValueError, AttributeError):
            check(False, field)
    def identity(value, field):
        fields(value, {'profile', 'base_url', 'model'}, field)
        text(value['profile'], field, True)
        check(PROFILE_NAME.fullmatch(value['profile']) is not None, field)
        check(_valid_url(value['base_url']), field)
        text(value['model'], field, True)
    check(isinstance(header, dict) and frozenset(header) in {frozenset(HEADER), frozenset(HEADER | {'compaction'})}, 'header fields')
    check(header['type'] == 'session', 'type')
    check(type(header['schema_version']) is int and header['schema_version'] == 1, 'schema_version')
    try:
        uid = UUID(header['session_id'])
        check(uid.version == 4 and str(uid) == header['session_id'], 'session_id')
    except (ValueError, TypeError, AttributeError):
        check(False, 'session_id')
    text(header['project_root'], 'project_root', True)
    check(header['project_root'] == str(root.resolve()), 'project_root (different project)')
    date(header['created_at'], 'created_at')
    date(header['saved_at'], 'saved_at')
    if header['name'] is not None:
        text(header['name'], 'name', True)
    identity(header['initial_model'], 'initial_model')
    integer(header['event_count'], 'event_count')
    check(header['event_count'] == len(events), 'event_count')
    if 'compaction' in header:
        compaction = header['compaction']
        fields(compaction, {'version', 'through_seq', 'summary'}, 'compaction')
        check(type(compaction['version']) is int and compaction['version'] == 1, 'compaction.version')
        integer(compaction['through_seq'], 'compaction.through_seq', 1)
        check(compaction['through_seq'] < len(events), 'compaction.through_seq')
        text(compaction['summary'], 'compaction.summary', True)
        check(len(compaction['summary'].encode('utf-8')) <= 1_000_000, 'compaction.summary')
        check(events[compaction['through_seq']].get('role') == 'user'
              and events[compaction['through_seq']].get('type') == 'message', 'compaction boundary')
    current_model = header['initial_model']
    active = None
    calls = {}
    results = set()
    batch = []
    assistant_seq = None
    for index, record in enumerate(events, 1):
        line = index + 1
        check(isinstance(record, dict), 'event')
        kind = record.get('type')
        check(isinstance(kind, str) and kind in PAYLOADS, 'type')
        allowed = {'type', 'seq', 'at'} | PAYLOADS[kind]
        if kind == 'approval' and 'authorization' in record:
            allowed = allowed | {'authorization'}
        fields(record, allowed, 'event fields')
        integer(record['seq'], 'seq', 1)
        check(record['seq'] == index, 'seq')
        date(record['at'], 'at')
        if batch:
            check(kind == 'tool_call' and index == batch[0], 'tool batch adjacency')
        if kind == 'message' and record['role'] == 'user':
            check(active is None, 'turn (previous turn unfinished)')
            active = index
        if 'turn' in record:
            if record['turn'] is None:
                check(kind in {'notice', 'approval'} and active is None, 'turn')
            else:
                integer(record['turn'], 'turn', 1)
                check(record['turn'] == active, 'turn')
        if kind == 'message':
            enum(record['role'], {'user', 'assistant'}, 'role')
            text(record['text'], 'text')
            enum(record['completion'], {'complete', 'interrupted'}, 'completion')
            check(active is not None and not (set(calls) - results), 'turn (pending calls)')
            check(isinstance(record['call_seqs'], list), 'call_seqs')
            for seq in record['call_seqs']:
                integer(seq, 'call_seqs', 1)
            if record['role'] == 'user':
                check(record['completion'] == 'complete' and not record['call_seqs'], 'user message')
            elif record['completion'] == 'interrupted':
                check(not record['call_seqs'], 'interrupted call_seqs')
            else:
                batch = list(record['call_seqs'])
                check(batch == list(range(index + 1, index + 1 + len(batch))), 'call_seqs')
                assistant_seq = index
        elif kind == 'tool_call':
            check(bool(batch), 'tool_call without assistant batch')
            batch.pop(0)
            integer(record['assistant_seq'], 'assistant_seq', 1)
            check(record['assistant_seq'] == assistant_seq, 'assistant_seq')
            text(record['name'], 'name', True)
            check(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,63}', record['name']) is not None, 'name')
            check(isinstance(record['arguments'], dict), 'arguments')
            calls[index] = active
        elif kind == 'approval':
            if 'authorization' in record:
                authorization = record['authorization']
                fields(authorization, {'source', 'grant_id'}, 'authorization')
                enum(authorization['source'], {'review', 'session_grant'}, 'authorization.source')
                if authorization['source'] == 'review':
                    check(authorization['grant_id'] is None, 'authorization.grant_id')
                else:
                    text(authorization['grant_id'], 'authorization.grant_id', True)
                    check(re.fullmatch('[1-9][0-9]*', authorization['grant_id']) is not None, 'authorization.grant_id')
                    check(record['decision'] == 'approved' and record['action'] in {'patch', 'command'}, 'grant authorization')
            if record['call_seq'] is not None:
                integer(record['call_seq'], 'call_seq', 1)
                check(record['call_seq'] in calls and calls[record['call_seq']] == active and record['call_seq'] not in results, 'call_seq')
            enum(record['action'], {'read','patch','command','checkpoint','commit','undo','fetch'}, 'action')
            enum(record['decision'], {'approved','denied','cancelled'}, 'decision')
            check(isinstance(record['reasons'], list) and bool(record['reasons']), 'reasons')
            for reason in record['reasons']:
                enum(reason, {'sensitive','outside_root','write','execute','git_change','network'}, 'reasons')
            check(isinstance(record['targets'], list), 'targets')
            for target in record['targets']:
                text(target, 'targets', True)
                check(Path(target).is_absolute(), 'targets')
            text(record['summary'], 'summary')
            text(record['review_sha256'], 'review_sha256')
            check(re.fullmatch('[0-9a-f]{64}', record['review_sha256']) is not None, 'review_sha256')
        elif kind == 'tool_result':
            integer(record['call_seq'], 'call_seq', 1)
            seq = record['call_seq']
            check(seq in calls and calls[seq] == active and seq not in results, 'call_seq')
            results.add(seq)
            enum(record['status'], {'ok','error','denied','cancelled','timeout','not_run'}, 'status')
            text(record['model_text'], 'model_text')
            check(isinstance(record['data'], dict), 'data')
            if record['error'] is not None:
                fields(record['error'], {'code','message','retryable'}, 'error')
                text(record['error']['code'], 'error.code', True)
                text(record['error']['message'], 'error.message')
                check(type(record['error']['retryable']) is bool, 'error.retryable')
            output = record['output']
            if output is not None:
                fields(output, {'chunks','observed_bytes','retained_bytes','model_max_bytes','retain_max_bytes','capture_truncated','model_truncated'}, 'output')
                for field in ('observed_bytes','retained_bytes','model_max_bytes','retain_max_bytes'):
                    integer(output[field], 'output.' + field, 1 if field.endswith('max_bytes') else 0)
                check(output['retained_bytes'] <= min(output['observed_bytes'], output['retain_max_bytes']), 'output.retained_bytes')
                check(output['model_max_bytes'] <= output['retain_max_bytes'], 'output.model_max_bytes')
                for field in ('capture_truncated','model_truncated'):
                    check(type(output[field]) is bool, 'output.' + field)
                check(output['capture_truncated'] == (output['retained_bytes'] < output['observed_bytes']), 'output.capture_truncated')
                check(not output['capture_truncated'] or output['model_truncated'], 'output.model_truncated')
                check(isinstance(output['chunks'], list), 'output.chunks')
                omitted = 0
                for chunk in output['chunks']:
                    check(isinstance(chunk, dict), 'output.chunks')
                    if set(chunk) == {'omitted_bytes'}:
                        integer(chunk['omitted_bytes'], 'output.omitted_bytes', 1)
                        omitted += chunk['omitted_bytes']
                    else:
                        fields(chunk, {'stream','text'}, 'output.chunks')
                        enum(chunk['stream'], {'stdout','stderr'}, 'output.stream')
                        text(chunk['text'], 'output.text')
                check(omitted == output['observed_bytes'] - output['retained_bytes'], 'output.omitted_bytes')
        elif kind == 'model_changed':
            check(active is None, 'model_changed during turn')
            identity(record['previous'], 'previous')
            identity(record['current'], 'current')
            check(record['previous'] == current_model, 'previous')
            check(record['confirmed'] is True, 'confirmed')
            enum(record['reason'], {'resume','user_selection'}, 'reason')
            current_model = record['current']
        elif kind == 'turn_end':
            check(active is not None and not (set(calls) - results), 'turn_end (pending calls)')
            enum(record['status'], {'complete','cancelled','error'}, 'status')
            if record['reason'] is not None:
                text(record['reason'], 'reason', True)
            active = None
        elif kind == 'notice':
            text(record['code'], 'code', True)
            text(record['text'], 'text')
    line = len(events) + 1
    check(active is None and not batch and set(calls) == results, 'unfinished turn or tool batch')


def decode(data: bytes, root: Path) -> tuple[dict, list[dict]]:
    if len(data) > MAX_FILE_BYTES:
        raise StorageError('File exceeds the 64 MB session limit.')
    lines = data.splitlines(keepends=True)
    if not lines:
        raise StorageError('line 1: missing session header.')
    records = []
    for index, line in enumerate(lines, 1):
        if len(line) > MAX_LINE_BYTES or not line.endswith(b'\n') or not line.strip():
            raise StorageError(f'line {index}: oversized, blank, or truncated record.')
        records.append(_parse(line, index))
    validate(records[0], records[1:], root)
    return records[0], records[1:]


def _header(session: Session, name: str | None):
    header = {'type':'session', 'schema_version':1, 'session_id':session.session_id,
            'project_root':str(session.project_root), 'created_at':session.created_at,
            'saved_at':timestamp(), 'name':name, 'initial_model':asdict(session.initial_model),
            'event_count':len(session.events)}
    if session.compaction is not None:
        header['compaction'] = dict(session.compaction)
    return header


def encoded(session: Session, name: str | None) -> bytes:
    if session.active_turn is not None:
        raise StorageError('Wait for active work and cancellation cleanup before saving or exporting.')
    header = _header(session, name)
    try:
        _json_values([header, *session.events])
    except (ValueError, UnicodeError, RecursionError):
        raise StorageError('Session contains invalid or excessively nested JSON values.') from None
    validate(header, session.events, session.project_root)
    try:
        data = ''.join(json.dumps(record, ensure_ascii=True, allow_nan=False) + '\n'
                       for record in [header, *session.events]).encode()
    except (ValueError, TypeError, RecursionError):
        raise StorageError('Session contains invalid JSON values.') from None
    decode(data, session.project_root)  # Enforce the same reader size/encoding limits.
    return data


def atomic_write(path: Path, data: bytes, expected: bytes | None):
    temporary = None
    try:
        if snapshot(path) != expected:
            raise Conflict('Destination changed since review; retry and review it again.')
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.skua-', suffix='.tmp', dir=path.parent)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if snapshot(path) != expected:
            raise Conflict('Destination changed since review; retry and review it again.')
        os.replace(temporary, path)
        temporary = None
    except OSError:
        raise StorageError('Could not write destination; previous file was not replaced.') from None
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


class Store:
    def __init__(self, root: Path, directory: Path):
        self.root = root.resolve()
        self.directory = directory.resolve()

    def destination(self, session: Session, path=None) -> Path:
        return path_for(self.root, path) if path is not None else (session.destination or self.directory / f'{session.session_id}.jsonl')

    def acquire(self, session: Session):
        if session.project_root != self.root:
            raise StorageError('Session belongs to a different project.')
        if session.lock is None:
            session.lock = SessionLock(self.root, 'session:' + session.session_id)

    def lookup(self, value: str) -> Path:
        try:
            uid = UUID(value)
            if str(uid) == value and uid.version == 4:
                matches = []
                for path in self.directory.glob('*.jsonl'):
                    data = snapshot(path)
                    if data is None:
                        continue
                    try:
                        header, _ = decode(data, self.root)
                    except StorageError:
                        continue
                    if header['session_id'] == value:
                        matches.append(path)
                if len(matches) != 1:
                    raise StorageError('Session ID is missing or ambiguous; use an explicit JSONL path.')
                return matches[0].resolve()
        except ValueError:
            pass
        return path_for(self.root, value)

    def load(self, value: str) -> Session:
        path = self.lookup(value)
        data = snapshot(path)
        if data is None:
            raise StorageError('Session file does not exist.')
        header, events = decode(data, self.root)
        session = Session(self.root, ModelIdentity(**header['initial_model']))
        session.session_id = header['session_id']
        self.acquire(session)
        try:
            if snapshot(path) != data:
                raise Conflict('Session changed during resume; retry.')
            session.created_at = header['created_at']
            session.name = header['name']
            session.events = events
            session.compaction = header.get('compaction')
            session.destination = path
            session.saved_bytes = data
            session.mark_saved()
            return session
        except BaseException:
            session.close()
            raise

    def list_sessions(self) -> list[str]:
        rows = []
        try:
            paths = sorted(self.directory.glob('*.jsonl'))
            for path in paths:
                try:
                    data = snapshot(path)
                    if data is None:
                        continue
                    header, _ = decode(data, self.root)
                    rows.append(f"{header['session_id']}  {header['name'] or '(unnamed)'}  {header['saved_at']}  {path.name}")
                except StorageError as exc:
                    rows.append(f'{path.name}: unavailable ({exc})')
        except OSError:
            raise StorageError('Could not list session directory.') from None
        return rows

    def save(self, session: Session, path: Path, expected: bytes | None, *, overwrite=False, name=None):
        path = path_for(self.root, path)
        chosen_name = session.name if name is None else name
        data = encoded(session, chosen_name)
        self.acquire(session)
        with SessionLock(self.root, 'path:' + str(path)):
            own = path == session.destination and expected == session.saved_bytes
            if expected is not None and not own and not overwrite:
                raise Conflict('Destination exists; explicit replacement approval is required.')
            other_lock = None
            try:
                if expected is not None and not own:
                    try:
                        first = _parse(expected.splitlines()[0], 1)
                    except (StorageError, IndexError):
                        first = None
                    if (isinstance(first, dict) and first.get('type') == 'session'
                            and first.get('project_root') != str(self.root)):
                        raise StorageError('Cannot replace a session belonging to another project; save elsewhere.')
                    try:
                        other, _ = decode(expected, self.root)
                    except StorageError:
                        other = None
                    if other and other['session_id'] != session.session_id:
                        other_lock = SessionLock(self.root, 'session:' + other['session_id'])
                atomic_write(path, data, expected)
                session.name = chosen_name
                session.destination = path
                session.saved_bytes = data
                session.mark_saved()
            finally:
                if other_lock:
                    other_lock.close()

    def export(self, session: Session, path: Path, expected: bytes | None, *, overwrite=False):
        path = path_for(self.root, path)
        encoded(session, session.name)  # Stable, structurally valid state; never writes JSONL.
        if path.suffix.lower() != '.md' or path == session.destination:
            raise StorageError('Markdown exports require a .md path distinct from the JSONL destination.')
        own = owns_export(session, path, expected)
        if expected is not None and not own and not overwrite:
            raise Conflict('Export destination exists; explicit replacement approval is required.')
        if expected:
            try:
                first = _parse(expected.splitlines()[0], 1)
            except StorageError:
                first = None
            if isinstance(first, dict) and first.get('type') == 'session':
                raise StorageError('Cannot replace canonical session records with an export.')
        data = markdown(session).encode('utf-8')
        if len(data) > MAX_FILE_BYTES:
            raise StorageError('Export exceeds the 64 MB limit.')
        with SessionLock(self.root, 'path:' + str(path)):
            atomic_write(path, data, expected)
        session.exports[str(path)] = data


def markdown(session: Session) -> str:
    parts = [f'<!-- skua-session: {session.session_id} -->\n# Skua session\n\nSession: {session.session_id}\n']
    if session.compaction:
        parts.append(f"\nCompacted active context through event {session.compaction['through_seq']}; full canonical history follows.\n")
    if session.name:
        parts.append(f'\n<pre>Name: {html.escape(session.name)}</pre>\n')
    for event in session.events:
        kind = event['type']
        if kind == 'message':
            label = event['role'].title() + (' (interrupted)' if event['completion'] == 'interrupted' else '')
            parts.append(f'\n## {label}\n\n<pre>{html.escape(event["text"])}</pre>\n')
        elif kind in {'tool_call', 'tool_result', 'approval'}:
            content = html.escape(json.dumps(event, ensure_ascii=False, indent=2))
            parts.append(f'\n<details>\n<summary>{kind.replace("_", " ").title()} {event["seq"]}</summary>\n\n<pre>{content}</pre>\n</details>\n')
        elif kind == 'notice':
            parts.append(f'\n<pre>Notice: {html.escape(event["text"])}</pre>\n')
    return ''.join(parts)


def owns_export(session: Session, path: Path, data: bytes | None) -> bool:
    if data is None:
        return False
    if str(path) in session.exports:
        return session.exports[str(path)] == data
    return data.startswith(f'<!-- skua-session: {session.session_id} -->\n'.encode())
