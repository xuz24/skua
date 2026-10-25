"""M4: bounded automatic compaction and shared manual behavior."""
import asyncio
from copy import deepcopy

import pytest

from skua.agent import run_turn
from skua.client import ModelError, Reply, ToolCall
from skua.config import ConfigError
from skua.context import compact
from skua.storage import Store, decode, encoded
from skua.tools import Runtime
from test_tools import config, state, Client


def history(root):
    session = state(root)
    for i in range(5):
        session.begin(f'question {i} ' + 'detail ' * 200)
        session.finish(f'answer {i} ' + 'context ' * 200)
    return session


def automatic(root):
    return config(root, context={'budget_tokens': 100, 'auto_compact': True})


def test_config_requires_explicit_budget_and_valid_threshold(workspace):
    assert config(workspace).context.auto_compact is False
    assert config(workspace).context.budget_tokens is None
    for context in [{'auto_compact': True}, {'budget_tokens': 0}, {'budget_tokens': True},
                    {'threshold_percent': 0}, {'threshold_percent': 101},
                    {'threshold_percent': .8}, {'auto_compact': 'true'}]:
        with pytest.raises(ConfigError):
            config(workspace, context=context)
    assert automatic(workspace).context.threshold_percent == 80


def test_auto_compacts_once_and_preserves_canonical_history(workspace):
    session = history(workspace)
    old = deepcopy(session.events)
    client = Client(Reply('Earlier work summary.'),
                    Reply('', (ToolCall('list_files', {'path': '.'}),)), Reply('done'))
    notices, activity = [], []
    runtime = Runtime(automatic(workspace), on_activity=activity.append)
    asyncio.run(run_turn(session, client, 'continue', runtime=runtime, on_notice=notices.append))
    assert session.events[:len(old)] == old
    assert len(client.requests) == 3  # No repeated compaction between tools/model replies.
    assert 'tools' not in client.requests[0][1]
    assert client.requests[1][0][1]['content'].startswith('Summary of earlier conversation')
    assert [m['content'].split()[1] for m in [m for m in client.requests[1][0] if m['role'] == 'user'][:2]] == ['3', '4']
    assert any(e and e.phase == 'compaction' for e in activity)
    assert any('above the configured threshold' in n for n in notices)
    decode(encoded(session, None), workspace)
    assert not list(workspace.iterdir())  # No automatic session save.


@pytest.mark.parametrize('response', [Reply(''), Reply('', (ToolCall('list_files', {'path': '.'}),)),
                                      Reply('x' * 1_000_001), ModelError('invalid_response', 'bad reply')])
def test_failed_auto_compaction_preserves_context_and_never_loops(workspace, response):
    session = history(workspace)
    original = deepcopy(session.events)
    client = Client(response, Reply('', (ToolCall('list_files', {'path': '.'}),)), Reply('done'))
    notices = []
    asyncio.run(run_turn(session, client, 'continue', runtime=Runtime(automatic(workspace)), on_notice=notices.append))
    assert session.compaction is None
    assert session.events[:len(original)] == original
    assert len(client.requests) == 3
    assert any('no more attempts this turn' in n for n in notices)
    assert client.requests[1][0][1]['content'] == original[0]['text']


def test_nonshrinking_summary_and_timeout_preserve_previous_summary(workspace, monkeypatch):
    session = history(workspace)
    asyncio.run(compact(session, Client(Reply('Earlier work.'))))
    session.begin('another ' * 200)
    session.finish('answer ' * 200)
    previous = deepcopy(session.compaction)
    with pytest.raises(ValueError, match='did not reduce'):
        asyncio.run(compact(session, Client(Reply('long ' * 10000))))
    assert session.compaction == previous
    monkeypatch.setattr('skua.context.COMPACTION_TIMEOUT', .01)
    class Slow:
        async def complete(self, *args):
            await asyncio.Event().wait()
    activity = []
    with pytest.raises(TimeoutError):
        asyncio.run(compact(session, Slow(), on_activity=activity.append))
    assert session.compaction == previous and activity[-1] is None


def test_cancel_auto_stops_turn_preserving_history_and_clearing_activity(workspace):
    async def scenario():
        session = history(workspace)
        previous = deepcopy(session.events)
        started = asyncio.Event()
        class Slow:
            async def complete(self, *args, **kwargs):
                started.set()
                await asyncio.Event().wait()
        activity = []
        task = asyncio.create_task(run_turn(session, Slow(), 'continue',
                                  runtime=Runtime(automatic(workspace), on_activity=activity.append)))
        await started.wait()
        task.cancel()
        await task
        assert session.compaction is None
        assert session.events[:len(previous)] == previous
        assert session.events[-1]['status'] == 'cancelled'
        assert activity[-1] is None
        decode(encoded(session, None), workspace)
    asyncio.run(scenario())


def test_auto_keeps_recent_tool_groups_and_active_batch_intact(workspace):
    session = history(workspace)
    # A real tool turn must stay intact among the two retained completed turns.
    asyncio.run(run_turn(session, Client(Reply('', (ToolCall('list_files', {'path': '.'}),)), Reply('listed')),
                         'list', runtime=Runtime(config(workspace))))
    client = Client(Reply('Summary.'), Reply('done'))
    asyncio.run(run_turn(session, client, 'next', runtime=Runtime(automatic(workspace))))
    messages = client.requests[-1][0]
    calls = [c['id'] for m in messages for c in m.get('tool_calls', [])]
    results = [m['tool_call_id'] for m in messages if m['role'] == 'tool']
    assert calls == results and len(calls) == 1
    decode(encoded(session, None), workspace)


def test_manual_off_and_save_resume_roundtrip(workspace):
    session = history(workspace)
    cfg = config(workspace)
    client = Client(Reply('ordinary reply'))
    asyncio.run(run_turn(session, client, 'next', runtime=Runtime(cfg)))
    assert session.compaction is None and len(client.requests) == 1
    asyncio.run(compact(session, Client(Reply('Saved summary.'))))
    old = deepcopy(session.events)
    path = workspace / 'saved.jsonl'
    path.write_bytes(encoded(session, None))
    restored = Store(workspace, workspace / '.skua/sessions').load(str(path))
    try:
        assert restored.events == old and restored.compaction == session.compaction
        before = path.read_bytes()
        asyncio.run(run_turn(restored, Client(Reply('New summary.'), Reply('continued')), 'next',
                             runtime=Runtime(automatic(workspace))))
        assert path.read_bytes() == before
        decode(encoded(restored, None), workspace)
    finally:
        restored.close()


def test_short_history_skips_without_summary_request(workspace):
    session = state(workspace)
    client = Client(Reply('done'))
    notices = []
    asyncio.run(run_turn(session, client, 'large input ' * 1000, runtime=Runtime(automatic(workspace)), on_notice=notices.append))
    assert len(client.requests) == 1 and session.compaction is None
    assert any('not enough older' in n.lower() for n in notices)


def test_manual_terminal_uses_shared_compaction_and_reports_budget(workspace):
    from test_session_transitions import UI, config as chat_config
    from skua.terminal import conversation
    session = history(workspace)
    ui = UI(['/context', '/compact', '/exit', 'discard'])
    asyncio.run(conversation(chat_config(workspace), Client(Reply('Manual summary.')), ui, initial=session))
    assert session.compaction['summary'] == 'Manual summary.'
    assert any('provider usage and limit unknown' in text for text in ui.output)
    assert any('automatic compaction: off' in text for text in ui.output)


def test_threshold_crossed_after_tool_keeps_active_results(workspace):
    from skua.terminal import context_estimate
    session = history(workspace)
    budget = context_estimate(session, Runtime(config(workspace))) + 300
    cfg = config(workspace, context={'budget_tokens': budget, 'auto_compact': True, 'threshold_percent': 100})
    (workspace / 'large').write_text('x' * 19000)
    client = Client(Reply('', (ToolCall('read_file', {'path': 'large'}),)), Reply('Summary.'), Reply('done'))
    asyncio.run(run_turn(session, client, 'read large', runtime=Runtime(cfg)))
    assert 'tools' in client.requests[0][1] and 'tools' not in client.requests[1][1]
    assert session.compaction is not None
    final = client.requests[2][0]
    calls = [c['id'] for m in final for c in m.get('tool_calls', [])]
    assert calls == [m['tool_call_id'] for m in final if m['role'] == 'tool']
    assert any('x' * 19000 in m['content'] for m in final if m['role'] == 'tool')
    assert not any(m['role'] == 'tool' for m in client.requests[1][0])
    decode(encoded(session, None), workspace)


def test_below_threshold_does_not_compact(workspace):
    session = history(workspace)
    cfg = config(workspace, context={'budget_tokens': 1000000, 'auto_compact': True})
    client = Client(Reply('done'))
    asyncio.run(run_turn(session, client, 'continue', runtime=Runtime(cfg)))
    assert len(client.requests) == 1 and session.compaction is None


def test_manual_ctrl_c_preserves_context_and_returns_prompt(workspace):
    import signal
    from test_session_transitions import UI, config as chat_config
    from skua.terminal import conversation
    session = history(workspace)
    old = deepcopy(session.events)
    class Slow:
        async def complete(self, *args):
            asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGINT)
            await asyncio.Event().wait()
    ui = UI(['/compact', '/exit', 'discard'])
    asyncio.run(conversation(chat_config(workspace), Slow(), ui, initial=session))
    assert session.events == old and session.compaction is None and ui.activity is None
    assert any('Compaction cancelled' in text for text in ui.output)
