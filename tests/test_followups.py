import asyncio
import io
import json

import pytest
from prompt_toolkit.document import Document
from rich.console import Console
from rich.style import Style

from skua.config import ConfigError, load_config, parse_config
from skua.session import ModelIdentity, Session
from skua.startup import _asset, _seen, maybe_play
from skua.storage import Store, StorageError, decode, encoded, snapshot
from skua.terminal import ProjectPathCompleter


def test_compaction_context_keeps_recent_complete_turns_and_roundtrips(workspace):
    session = Session(workspace, ModelIdentity('default', 'https://example/v1', 'model'))
    for index in range(4):
        session.begin(f'user {index}')
        session.finish(f'assistant {index}')
    messages, through = session.compaction_input()
    assert messages[0] == {'role': 'user', 'content': 'user 0'}
    assert messages[-1] == {'role': 'assistant', 'content': 'assistant 1'}
    session.compaction = {'version': 1, 'through_seq': through, 'summary': 'Earlier work summary.'}
    assert session.active_messages()[0]['role'] == 'system'
    assert [m['content'] for m in session.active_messages() if m['role'] == 'user'] == ['user 2', 'user 3']
    data = encoded(session, None)
    header, events = decode(data, workspace)
    assert header['compaction'] == session.compaction
    path = workspace / 'history.jsonl'
    path.write_bytes(data)
    restored = Store(workspace, workspace / '.skua/sessions').load(str(path))
    try:
        assert restored.compaction == session.compaction
        assert restored.events == session.events
        assert not restored.dirty
    finally:
        restored.close()


def test_compaction_rejects_boundary_inside_turn(workspace):
    session = Session(workspace, ModelIdentity('default', 'https://example/v1', 'model'))
    for index in range(3):
        session.begin(f'user {index}')
        session.finish(f'assistant {index}')
    session.compaction = {'version': 1, 'through_seq': 1, 'summary': 'bad boundary'}
    try:
        try:
            encoded(session, None)
        except StorageError as exc:
            assert 'boundary' in str(exc)
        else:
            raise AssertionError('expected invalid compaction boundary')
    finally:
        session.close()


def test_compaction_input_keeps_tool_call_and_result_together(workspace):
    session = Session(workspace, ModelIdentity('default', 'https://example/v1', 'model'))
    session.begin('inspect a file')
    session._append('message', role='assistant', text='I will read it.', completion='complete', turn=1, call_seqs=[3])
    session._append('tool_call', turn=1, assistant_seq=2, name='read_file', arguments={'path': 'a.txt'})
    session._append('approval', turn=1, call_seq=3, action='read', decision='approved', reasons=['read'],
                    targets=['a.txt'], summary='Read a.txt', review_sha256='0' * 64)
    session._append('tool_result', turn=1, call_seq=3, status='ok', model_text='file contents',
                    output=None, data={'path': 'a.txt', 'text': 'file contents'}, error=None)
    session.finish('The file says hello.')
    for index in (2, 3):
        session.begin(f'user {index}')
        session.finish(f'assistant {index}')
    older, through = session.compaction_input()
    assistant = next(message for message in older if message.get('tool_calls'))
    result = next(message for message in older if message['role'] == 'tool')
    assert assistant['tool_calls'][0]['id'] == result['tool_call_id'] == 'skua_call_3'
    assert session.events[through - 1]['type'] == 'turn_end'


def test_path_completion_lists_names_without_sensitive_or_ignored_files(workspace):
    (workspace / '.gitignore').write_text('ignored.txt\n')
    (workspace / 'visible.txt').write_text('contents must not be read')
    (workspace / 'secret.key').write_text('secret')
    (workspace / 'ignored.txt').write_text('ignored')
    config = load_config(workspace, project=False, global_path=workspace / 'missing.toml')
    completer = ProjectPathCompleter(workspace, config)
    completions = list(completer.get_completions(Document('vis'), None))
    assert [item.text for item in completions] == ['visible.txt']
    assert list(completer.get_completions(Document('secret'), None)) == []
    assert list(completer.get_completions(Document('ignored'), None)) == []


def test_path_completion_does_not_follow_external_links_or_read_contents(workspace, tmp_path, monkeypatch):
    from skua.permissions import Target
    outside = tmp_path / 'outside-file.txt'
    outside.write_text('must not be read')
    link = workspace / 'external-link.txt'
    link.symlink_to(outside)
    monkeypatch.setattr(Target, 'read', lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('read file content')))
    completer = ProjectPathCompleter(workspace)
    assert list(completer.get_completions(Document('external'), None)) == []


def test_startup_animation_asset_and_global_once_marker(workspace, monkeypatch):
    raw, frames = _asset()
    assert len(raw) < 1_000_000 and len(frames) == 7
    for source, (rows, _) in zip(json.loads(raw)['frames'], frames):
        for cells, row in zip(source['cells'], rows):
            for cell, span in zip(cells, row.spans):
                style = Style.parse(span.style)
                if cell['bg'] == [0, 0, 0]:
                    assert style.bgcolor is None
                else:
                    assert tuple(style.bgcolor.triplet) == tuple(cell['bg'])
    path = workspace / 'state/startup.json'
    monkeypatch.setattr('skua.startup._state_path', lambda: path)
    monkeypatch.setattr('skua.startup.sys.stdout', type('TTY', (), {'isatty': lambda self: True})())
    async def no_wait(_):
        return None
    monkeypatch.setattr('asyncio.sleep', no_wait)
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=100)
    assert asyncio.run(maybe_play('once', console)) is True
    assert asyncio.run(maybe_play('once', console)) is False
    assert asyncio.run(maybe_play('always', console)) is True
    assert asyncio.run(maybe_play('never', console)) is False
    rendered = output.getvalue()
    assert '\x1b[?1049' not in rendered and '\x1b[2J' not in rendered
    assert '\x1b[2K' in rendered  # Erase individual animation rows.
    assert rendered.count('\x1b[?25h') == 2  # Cursor restored after each playback.
    assert json.loads(path.read_text())['version'] == 1


@pytest.mark.parametrize('error', [OSError, asyncio.CancelledError])
def test_startup_restores_screen_after_failed_or_cancelled_playback(workspace, monkeypatch, error):
    path = workspace / 'startup.json'
    monkeypatch.setattr('skua.startup._state_path', lambda: path)
    monkeypatch.setattr('skua.startup.sys.stdout', type('TTY', (), {'isatty': lambda self: True})())
    async def interrupted(_):
        raise error()
    monkeypatch.setattr('asyncio.sleep', interrupted)
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=100)
    if error is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(maybe_play('once', console))
    else:
        assert asyncio.run(maybe_play('once', console)) is False
    assert '\x1b[?1049' not in output.getvalue()
    assert '\x1b[?25h' in output.getvalue()
    assert '\x1b[2K' in output.getvalue()
    assert not path.exists()


@pytest.mark.parametrize('data', [[], None, {'seen': None}, {'seen': 'installation-key'}])
def test_startup_ignores_malformed_marker(workspace, data):
    path = workspace / 'startup.json'
    path.write_text(json.dumps(data))
    assert not _seen(path, 'installation-key')


def test_startup_setting_validation_and_default():
    assert parse_config(b'schema_version = 1\n', 'test')['schema_version'] == 1
    assert parse_config(b'schema_version = 1\n[ui]\nstartup_animation = "always"\n', 'test')['ui']['startup_animation'] == 'always'
    try:
        parse_config(b'schema_version = 1\n[ui]\nstartup_animation = "sometimes"\n', 'test')
    except ConfigError as exc:
        assert 'ui.startup_animation' in str(exc)
    else:
        raise AssertionError('expected invalid setting')
