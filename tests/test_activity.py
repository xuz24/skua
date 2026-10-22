"""M1 acceptance: lifecycle, responsive progress, and streaming/review coexistence."""
import asyncio
from io import StringIO
import signal

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from skua.activity import Activity
from skua.agent import run_turn
from skua.client import Reply, ToolCall
from skua.terminal import Terminal, ActivityDisplay, cancel_on_sigint
from skua.tools import Runtime
from test_tools import config, state, results


@pytest.mark.parametrize('decision,command,expected', [
    ('approved', 'printf hello', 'ok'),
    ('approved', 'exit 7', 'error'),
    ('approved', 'sleep 30', 'timeout'),
    ('denied', 'touch forbidden', 'denied'),
    ('cancelled', 'touch forbidden', 'cancelled'),
])
def test_tool_activity_lifecycle(workspace, decision, command, expected):
    events = []
    async def approve(review):
        assert events[-1].phase == 'approval'
        return decision
    terminal = Terminal(output=DummyOutput(), console=Console(file=StringIO()))
    def activity(event):
        events.append(event)
        terminal.show_activity(event)
    runtime = Runtime(config(workspace, commands={'timeout_seconds': 1}), approve=approve, on_activity=activity)
    result = asyncio.run(runtime.execute(state(workspace), 3, 'run_command', {'command': command}))
    assert result.status == expected
    assert [e.phase for e in events] == ['tool', 'approval', 'tool', 'tool']
    assert events[0] == events[2]
    assert events[-1].status == expected
    assert events[-1].started == events[0].started
    assert runtime.activity is None
    assert terminal.activity is None and terminal.status is None
    assert not (workspace / 'forbidden').exists()
    if decision == 'approved':
        assert '/output 3' in events[-1].summary
        assert 'exit code' in events[-1].summary


def test_invalid_tool_and_unexpected_exception_clear_activity(workspace, monkeypatch):
    events = []
    runtime = Runtime(config(workspace), on_activity=events.append)
    result = asyncio.run(runtime.execute(state(workspace), 3, 'missing', {}))
    assert result.status == events[-1].status == 'error'
    async def broken(*args):
        raise ValueError('unexpected')
    monkeypatch.setattr(runtime, '_execute', broken)
    with pytest.raises(ValueError):
        asyncio.run(runtime.execute(state(workspace), 4, 'list_files', {'path': '.'}))
    assert events[-1].status == 'error' and runtime.activity is None


def test_slow_command_refreshes_and_sigint_cleans_up(workspace):
    async def scenario():
        target = StringIO()
        terminal = Terminal(output=DummyOutput(), console=Console(file=target, force_terminal=True, width=100))
        async def approve(review):
            assert terminal.status is None
            assert 'Waiting for approval' in terminal._toolbar()
            return 'approved'
        events = []
        def activity(event):
            events.append(event)
            terminal.show_activity(event)
        runtime = Runtime(config(workspace), approve=approve, on_activity=activity)
        session = state(workspace)
        class Client:
            async def complete(self, *args, **kwargs):
                return Reply('', (ToolCall('run_command', {'command': 'printf started; touch started; sleep 30'}),))
        task = asyncio.create_task(run_turn(session, Client(), 'work', runtime=runtime))
        old = signal.getsignal(signal.SIGINT)
        with cancel_on_sigint(task):
            for _ in range(200):
                if (workspace / 'started').exists():
                    break
                await asyncio.sleep(.01)
            assert (workspace / 'started').exists()
            first = ActivityDisplay(terminal.activity).__rich__().plain
            boundary = len(target.getvalue())
            await asyncio.sleep(.3)
            assert ActivityDisplay(terminal.activity).__rich__().plain != first
            assert 'Running' in target.getvalue()[boundary:]
            signal.raise_signal(signal.SIGINT)
            await asyncio.wait_for(task, 8)
        assert signal.getsignal(signal.SIGINT) == old
        assert terminal.status is None and terminal.activity is None
        assert events[-1].status == 'cancelled'
        assert results(session)[0]['output']['retained_bytes'] == 7
        assert 'cancelled' in target.getvalue()
    asyncio.run(scenario())


@pytest.mark.parametrize("long_review", [False, True])
def test_ctrl_c_during_diff_approval_clears_activity(workspace, long_review):
    from test_patches import init, edit
    init(workspace)
    async def scenario():
        with create_pipe_input() as pipe:
            terminal = Terminal(input=pipe, output=DummyOutput(), console=Console(file=StringIO()))
            ready = asyncio.Event()
            async def approve(review):
                ready.set()
                return await terminal.approve(review)
            runtime = Runtime(config(workspace), approve=approve, on_activity=terminal.show_activity)
            content = ''.join(f'new line {i}\n' for i in range(100)) if long_review else 'new\n'
            task = asyncio.create_task(runtime.execute(state(workspace), 3, 'apply_patch',
                                       {'message': 'edit', 'changes': [edit(text=content)]}))
            await asyncio.wait_for(ready.wait(), 5)
            assert terminal.activity.phase == 'approval' and terminal.status is None
            pipe.send_text('\x03')
            assert (await task).status == 'cancelled'
            assert terminal.activity is None and terminal.status is None
            assert not terminal.reviewing
            assert (workspace / 'a.txt').read_text() == 'original\n'
    asyncio.run(scenario())


def test_streamed_edit_test_fix_flow_keeps_text_and_diff(workspace):
    from test_patches import init, edit
    init(workspace)
    target = StringIO()
    terminal = Terminal(output=DummyOutput(), console=Console(file=target, force_terminal=False, width=120))
    async def approve(review):
        assert terminal.status is None
        terminal.write_review(review.summary)
        return 'approved'
    runtime = Runtime(config(workspace, lint={'enabled': True, 'command': 'grep good a.txt'}),
                      approve=approve, on_notice=terminal.write, on_activity=terminal.show_activity)
    replies = iter([
        Reply('First edit.', (ToolCall('apply_patch', {'message': 'edit', 'changes': [edit(text='bad\n')]}),)),
        Reply('Fixing the failed test.', (ToolCall('apply_patch', {'message': 'fix', 'changes': [edit(text='good\n')]}),)),
        Reply('Tests now pass.'),
    ])
    class Client:
        async def complete(self, *args, **kwargs):
            reply = next(replies)
            for part in [reply.text[:5], reply.text[5:]]:
                kwargs['on_text'](part)
            return reply
    session = state(workspace)
    asyncio.run(run_turn(session, Client(), 'edit and test', runtime=runtime,
                         on_text=terminal.stream_reply, on_waiting=terminal.set_working))
    terminal.finish_reply()
    text = target.getvalue()
    for message in ['First edit.', 'Fixing the failed test.', 'Tests now pass.']:
        assert text.count(message) == 1
    assert text.index('First edit.') < text.index('apply_patch #3: error') < text.index('Fixing the failed test.')
    assert '-bad' in text and '+good' in text and '/output 3' in text
    assert [r['status'] for r in results(session)] == ['error', 'ok']
    assert results(session)[1]['output']['retained_bytes'] == 5
    assert f"/output {results(session)[1]['call_seq']}" in text
    assert terminal.activity is None and terminal.status is None
    assert (workspace / 'a.txt').read_text() == 'good\n'


def test_model_wait_restores_tool_and_approval_elapsed(monkeypatch):
    terminal = Terminal(output=DummyOutput(), console=Console(file=StringIO()))
    tool = Activity('tool', 'apply_patch', 3)
    terminal.show_activity(tool)
    terminal.set_working(True)
    assert terminal.activity.phase == 'model'
    terminal.set_working(False)
    assert terminal.activity is tool
    terminal.show_activity(Activity('approval', 'patch', 3, started=10))
    monkeypatch.setattr('skua.activity.monotonic', lambda: 12.5)
    assert '2.5s' in terminal._toolbar()
    assert terminal.status is None
    terminal.show_activity(None)
