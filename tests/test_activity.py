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
    assert result.status == expected, result
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
            await asyncio.sleep(1.1)
            assert ActivityDisplay(terminal.activity).__rich__().plain != first
            assert 'Running' in target.getvalue()[boundary:]
            signal.raise_signal(signal.SIGINT)
            await asyncio.wait_for(task, 8)
        assert signal.getsignal(signal.SIGINT) == old
        assert terminal.status is None and terminal.activity is None
        assert events[-1].status == 'cancelled', results(session)
        assert results(session)[0]['output']['retained_bytes'] == 7
        assert 'Cancelled · run_command' in target.getvalue()
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
    assert text.index('First edit.') < text.index('Failed · apply_patch: a.txt') < text.index('Fixing the failed test.')
    assert '-bad' in text and '+good' in text and '#3' in text
    assert [r['status'] for r in results(session)] == ['error', 'ok']
    assert results(session)[1]['output']['retained_bytes'] == 5
    assert f"#{results(session)[1]['call_seq']}" in text
    assert text.count('Use /output <number>') == 1
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


def test_command_block_previews_output_once_and_keeps_full_capture(workspace):
    target = StringIO()
    terminal = Terminal(output=DummyOutput(), console=Console(file=target, width=100))
    async def approve(review):
        return 'approved'
    runtime = Runtime(config(workspace), approve=approve, on_activity=terminal.show_activity,
                      on_notice=terminal.write)
    command = "printf 'line-%s\\n' {1..20}; printf 'problem\\n' >&2; exit 7"
    result = asyncio.run(runtime.execute(state(workspace), 3, 'run_command', {'command': command}))
    rendered = target.getvalue()
    assert 'Failed · run_command:' in rendered
    assert '  └ line-1\n' in rendered and 'stderr: problem' in rendered
    assert 'lines hidden' in rendered and '\n    line-10\n' not in rendered
    assert '#3' in rendered and rendered.count('Use /output <number>') == 1 and 'exit 7' in rendered
    assert 'line-10\n' in ''.join(c.get('text', '') for c in result.output['chunks'])


def test_successful_read_block_summarizes_without_echoing_contents(workspace):
    (workspace / 'notes.txt').write_text('private fixture body\n')
    target = StringIO()
    terminal = Terminal(output=DummyOutput(), console=Console(file=target))
    runtime = Runtime(config(workspace), on_activity=terminal.show_activity)
    result = asyncio.run(runtime.execute(state(workspace), 3, 'read_file', {'path': 'notes.txt'}))
    rendered = target.getvalue()
    assert '• Read notes.txt' in rendered and '1 total lines' in rendered
    assert 'private fixture body' not in rendered
    assert 'private fixture body' in result.text
    assert '• Read notes.txt · #3' in rendered


def test_denied_command_does_not_claim_to_have_run_or_show_output(workspace):
    target = StringIO()
    terminal = Terminal(output=DummyOutput(), console=Console(file=target))
    runtime = Runtime(config(workspace), on_activity=terminal.show_activity)
    result = asyncio.run(runtime.execute(state(workspace), 3, 'run_command', {'command': 'touch denied'}))
    rendered = target.getvalue()
    assert 'Denied · run_command: touch denied' in rendered and 'Do not retry' in rendered
    assert 'Ran ' not in rendered and '(no output)' not in rendered
    assert result.output is None and not (workspace / 'denied').exists()


def test_output_preview_bounds_long_lines_and_preserves_literal_markup():
    from skua.terminal import output_preview
    output = {'chunks': [{'stream': 'stdout', 'text': '[bold]literal[/bold]\n' + 'x' * 1000},
                         {'omitted_bytes': 42}, {'stream': 'stdout', 'text': '\x1b[31mend\n'}]}
    lines = output_preview(output, width=40)
    assert '[bold]literal[/bold]' in lines
    assert any('42 bytes not retained' in line for line in lines)
    assert all(len(line) <= 40 and '\x1b' not in line for line in lines)


@pytest.mark.parametrize('operation', ['read_file', 'search_files', 'list_files'])
def test_read_search_and_list_never_display_time_and_keep_number_in_heading(operation, monkeypatch):
    monkeypatch.setattr('skua.activity.monotonic', lambda: 12.5)
    activity = Activity('tool', operation, 9, started=0, status='ok',
                        arguments={'path': 'source', 'query': 'needle'},
                        result_text='saved result', data={'total_lines': 4})
    target = StringIO()
    terminal = Terminal(output=DummyOutput(), console=Console(file=target))
    terminal.show_activity(activity)
    rendered = target.getvalue()
    assert '#9' in rendered.splitlines()[0]
    assert '12.5s' not in rendered and '12.5s' not in ActivityDisplay(activity).__rich__().plain


@pytest.mark.parametrize('elapsed,expected', [(.9, False), (1.1, True)])
def test_command_time_only_appears_after_one_second(monkeypatch, elapsed, expected):
    monkeypatch.setattr('skua.activity.monotonic', lambda: elapsed)
    activity = Activity('tool', 'run_command', 3, started=0, status='ok',
                        arguments={'command': 'true'}, result_text='No output.')
    target = StringIO()
    terminal = Terminal(output=DummyOutput(), console=Console(file=target))
    terminal.show_activity(activity)
    assert (f'{elapsed:.1f}s' in target.getvalue()) is expected
    assert (f'{elapsed:.1f}s' in ActivityDisplay(activity).__rich__().plain) is expected


@pytest.mark.parametrize('cancel', [False, True])
def test_darwin_cleanup_race_keeps_tool_outcome_and_output(workspace, monkeypatch, cancel):
    import errno
    from skua import execution
    monkeypatch.setattr(execution.sys, 'platform', 'darwin')
    original = execution._signal_group
    pending = {signal.SIGTERM, 0}
    def race(pid, sig):
        if sig in pending:
            pending.remove(sig)
            raise PermissionError(errno.EPERM, 'unreaped group')
        return original(pid, sig)
    monkeypatch.setattr(execution, '_signal_group', race)
    async def scenario():
        async def yes(review):
            return 'approved'
        terminal = Terminal(output=DummyOutput(), console=Console(file=StringIO()))
        runtime = Runtime(config(workspace, commands={'timeout_seconds': 1}),
                          approve=yes, on_activity=terminal.show_activity)
        task = asyncio.create_task(runtime.execute(state(workspace), 3, 'run_command',
                                  {'command': 'printf started; touch started; sleep 30'}))
        if cancel:
            for _ in range(300):
                if (workspace / 'started').exists():
                    break
                await asyncio.sleep(.01)
            assert (workspace / 'started').exists()
            task.cancel()
        result = await asyncio.wait_for(task, 8)
        assert result.status == ('cancelled' if cancel else 'timeout'), result
        assert result.output['retained_bytes'] == 7
        assert terminal.activity is None and terminal.status is None
    asyncio.run(scenario())
