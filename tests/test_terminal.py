import asyncio
from io import StringIO

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from skua.client import Reply
from skua.config import build_config
from skua.terminal import Terminal, conversation


def test_bracketed_paste_and_alt_enter_require_submission():
    async def scenario():
        with create_pipe_input() as pipe:
            terminal = Terminal(input=pipe, output=DummyOutput())
            task = asyncio.create_task(terminal.read())
            await asyncio.sleep(0.03)
            pipe.send_text('\x1b[200~line one\nline two\x1b[201~')
            await asyncio.sleep(0.03)
            assert not task.done()
            assert terminal.prompt.default_buffer.text == 'line one\nline two'
            assert terminal.prompt.message == '❯ '
            assert terminal.prompt.layout.current_window.style() == 'class:input-field'
            height = terminal.prompt.layout.current_window.preferred_height(80, 40)
            assert height.min == height.max == 1
            pipe.send_text('\x1b\rthird')
            await asyncio.sleep(0.03)
            assert not task.done()
            pipe.send_text('\r')
            assert await task == 'line one\nline two\nthird'
    asyncio.run(scenario())


def test_draft_clear_and_intervening_input_reset_exit_sequence():
    async def scenario():
        with create_pipe_input() as pipe:
            terminal = Terminal(input=pipe, output=DummyOutput())
            task = asyncio.create_task(terminal.read())
            await asyncio.sleep(0.03)
            pipe.send_text('draft\x03')
            await asyncio.sleep(0.03)
            assert terminal.prompt.default_buffer.text == ''
            assert not terminal.exit_armed
            pipe.send_text('\x03')
            await asyncio.sleep(0.03)
            assert not task.done() and terminal.exit_armed
            # Navigation is input activity even without changing the draft.
            pipe.send_text('\x1b[D\x03')
            await asyncio.sleep(0.03)
            assert not task.done()
            pipe.send_text('\x03')
            assert await task == '/exit'
    asyncio.run(scenario())


def test_ctrl_d_requests_exit_only_on_empty_draft():
    async def scenario():
        with create_pipe_input() as pipe:
            terminal = Terminal(input=pipe, output=DummyOutput())
            task = asyncio.create_task(terminal.read())
            await asyncio.sleep(0.03)
            pipe.send_text('draft\x04')
            await asyncio.sleep(0.03)
            assert not task.done()
            pipe.send_text('\x03\x04')
            assert await task == '/exit'
    asyncio.run(scenario())


def test_discard_cancel_and_next_turn_keep_context(workspace):
    outputs = []
    responses = iter(['hello', '/exit', 'cancel', 'again', '/exit', 'discard'])
    class UI(Terminal):
        def __init__(self):
            pass
        async def read(self, message=''):
            return next(responses)
        def write(self, text, **kwargs):
            outputs.append(text)
    class Client:
        def __init__(self):
            self.requests = []
        async def complete(self, messages, **kwargs):
            self.requests.append(messages)
            kwargs['on_text']('answer')
            return Reply('answer')
    cfg = build_config(workspace, [('test', {'schema_version':1, 'profiles': {'default': {'base_url': 'https://example/v1', 'model':'model'}}})])
    client = Client()
    state = asyncio.run(conversation(cfg, client, UI()))
    assert len(client.requests) == 2
    assert [m['content'] for m in client.requests[1][1:]] == ['hello','answer','again']
    assert len([e for e in state.events if e['type']=='turn_end']) == 2
    assert list(workspace.iterdir()) == []


def test_output_is_plain_text_without_terminal_controls():
    target = StringIO()
    terminal = Terminal(console=Console(file=target, force_terminal=False), output=DummyOutput())
    terminal.write('[bold]literal\x1b[2J\x07')
    assert '\x1b' not in target.getvalue() and '\x07' not in target.getvalue()
    assert '[bold]literal' in target.getvalue()


def test_markdown_reply_flushes_before_notice_without_changing_literal_output():
    target = StringIO()
    terminal = Terminal(console=Console(file=target, force_terminal=False, width=80), output=DummyOutput())
    terminal.stream_reply('**Strong')
    terminal.stream_reply(' text**\n\n- List item\n\n```python\nprint("hello")\n```\x07')
    terminal.write('Notice: [bold]literal')
    terminal.finish_reply()
    rendered = target.getvalue()
    assert rendered.count('Strong text') == 1
    assert '**' not in rendered and '```' not in rendered and '\x07' not in rendered
    assert 'List item' in rendered and 'print("hello")' in rendered
    assert rendered.index('Strong text') < rendered.index('Notice: [bold]literal')


def test_streaming_markdown_cleanup_keeps_complete_long_reply():
    target = StringIO()
    terminal = Terminal(console=Console(file=target, force_terminal=True, width=40, height=4), output=DummyOutput())
    terminal.stream_reply('\n\n'.join(f'Paragraph {i}' for i in range(10)))
    boundary = len(target.getvalue())
    terminal.write('Response cancelled.')
    final = target.getvalue()[boundary:]
    assert terminal._reply_live is None
    assert '\x1b[?25h' in final
    assert '\x1b[?1049' not in target.getvalue()
    for i in range(10):
        assert final.count(f'Paragraph {i}') == 1
    assert final.index('Paragraph 9') < final.index('Response cancelled.')


def test_diff_color_preserves_review_text_and_pager_uses_same_colors():
    from prompt_toolkit.document import Document
    from prompt_toolkit.styles import Style
    from rich.text import Text
    from skua.terminal import ReviewLexer
    target = StringIO()
    terminal = Terminal(console=Console(file=target, force_terminal=True, color_system='truecolor', no_color=False, width=80), output=DummyOutput())
    review = 'Review [bold]literal\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+new\n context'
    terminal.write_review(review)
    rendered = Text.from_ansi(target.getvalue())
    assert '\n'.join(line.rstrip() for line in rendered.plain.splitlines()) == review
    for text, background in [('-old', (72, 35, 41)), ('+new', (22, 59, 43))]:
        start = rendered.plain.index(text)
        first = rendered.get_style_at_offset(terminal.console, start)
        last = rendered.get_style_at_offset(terminal.console, start + 79)
        assert tuple(first.bgcolor.triplet) == background
        assert first.bgcolor == last.bgcolor
        assert tuple(first.color.triplet) == (229, 231, 235)
    lexer = ReviewLexer(lambda: 80).lex_document(Document(review))
    assert lexer(4) == [('#e5e7eb bg:#482329', '-old'.ljust(80))]
    assert lexer(5) == [('#e5e7eb bg:#163b2b', '+new'.ljust(80))]
    for i in range(7):
        Style.from_dict({'line': lexer(i)[0][0]})


def test_terminal_enables_color_despite_inherited_no_color(monkeypatch):
    import skua.terminal as ui
    from prompt_toolkit.output.color_depth import ColorDepth
    monkeypatch.setenv('NO_COLOR', '1')
    target = StringIO()
    monkeypatch.setattr(ui, 'Console', lambda **kwargs: Console(
        file=target, force_terminal=True, color_system='truecolor', **kwargs))
    terminal = Terminal(output=DummyOutput())
    terminal.write_review('+added\n-removed')
    assert not terminal.console.no_color
    assert terminal.prompt.color_depth == ColorDepth.DEPTH_24_BIT
    assert '\x1b[38;2;229;231;235;48;2;22;59;43m' in target.getvalue()
    assert '\x1b[38;2;229;231;235;48;2;72;35;41m' in target.getvalue()


def test_diff_backgrounds_remain_colored_on_16_color_terminals():
    from rich.text import Text
    target = StringIO()
    console = Console(file=target, force_terminal=True, color_system='standard', no_color=False)
    terminal = Terminal(console=console, output=DummyOutput())
    terminal.write_review('+added\n-removed')
    rendered = Text.from_ansi(target.getvalue())
    assert rendered.get_style_at_offset(console, rendered.plain.index('+added')).bgcolor.number == 2
    assert rendered.get_style_at_offset(console, rendered.plain.index('-removed')).bgcolor.number == 1


def test_active_ctrl_c_returns_to_prompt(workspace):
    import signal
    cfg = build_config(workspace, [('test', {'schema_version':1,'profiles':{'default':{'base_url':'https://example/v1','model':'m'}}})])
    answers = iter(['hello', '/exit', 'discard'])
    output = []
    class UI(Terminal):
        def __init__(self):
            pass
        async def read(self, message=''):
            return next(answers)
        def write(self, text, **kwargs):
            output.append(text)
    class Client:
        async def complete(self, messages, **kwargs):
            kwargs['on_text']('partial')
            asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGINT)
            await asyncio.Event().wait()
    original = signal.getsignal(signal.SIGINT)
    state = asyncio.run(conversation(cfg, Client(), UI()))
    assert state.events[-1]['status'] == 'cancelled'
    assert state.events[-2]['text'] == 'partial'
    assert signal.getsignal(signal.SIGINT) == original
    assert any('Response cancelled.' in line for line in output)


def test_startup_capability_failure_never_opens_conversation(workspace):
    import httpx
    import pytest
    from skua.client import ChatClient, ModelError
    from skua.terminal import start
    from test_client import response_message
    cfg = build_config(workspace, [('test', {'schema_version':1,'profiles':{'default':{'base_url':'https://example/v1','model':'m','stream':False,'auth':{'enabled':False}}}})])
    class UI:
        def write(self, *args, **kwargs):
            pass
        async def read(self, *args):
            pytest.fail('conversation opened without native tool support')
    clients = []
    def factory(profile, credential):
        client = ChatClient(profile, credential, transport=httpx.MockTransport(lambda _:httpx.Response(200,json=response_message())))
        clients.append(client)
        return client
    with pytest.raises(ModelError, match='tool-calling'):
        asyncio.run(start(cfg,terminal=UI(),client_factory=factory))
    assert all(c.http.is_closed for c in clients)
    assert list(workspace.iterdir()) == []


def test_start_to_exit_with_actual_prompt_and_mock_endpoint(workspace):
    import httpx
    import json
    from skua.client import ChatClient
    from skua.terminal import start
    from test_client import Stream, event, sse, tool
    cfg = build_config(workspace, [('test', {'schema_version':1,'profiles':{'default':{'base_url':'https://example/v1','model':'m','auth':{'enabled':False}}}})])
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get('tools', [{}])[0].get('function', {}).get('name') == 'skua_diagnostic':
            body = sse(event({'tool_calls':[tool(0,'remote','skua_diagnostic','{"echo":"skua"}')]}), event(finish='tool_calls'))
        else:
            body = sse(event({'content':'Hello from mock.'}), event(finish='stop'))
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=Stream([body]))
    async def scenario():
        with create_pipe_input() as pipe:
            answers = iter(['hello\r','/exit\r','discard\r'])
            target = StringIO()
            class UI(Terminal):
                async def read(self, message='skua> '):
                    asyncio.get_running_loop().call_later(0.02, pipe.send_text, next(answers))
                    return await super().read(message)
            ui = UI(input=pipe,output=DummyOutput(),console=Console(file=target))
            await start(cfg,terminal=ui,client_factory=lambda p,k:ChatClient(p,k,transport=httpx.MockTransport(handler)))
            assert 'Hello from mock.' in target.getvalue()
            assert 'Unsaved conversation' in target.getvalue()
    asyncio.run(scenario())
    assert len(requests) == 2
    assert list(workspace.iterdir()) == []


def test_ctrl_c_cancels_review_on_first_press():
    from skua.permissions import Review
    async def scenario():
        with create_pipe_input() as pipe:
            terminal = Terminal(input=pipe, output=DummyOutput(), console=Console(file=StringIO()))
            review = Review.create('command', ['execute'], [], 'Review command', [])
            task = asyncio.create_task(terminal.approve(review))
            await asyncio.sleep(0.03)
            pipe.send_text('\x03')
            try:
                await task
            except asyncio.CancelledError:
                pass
            else:
                raise AssertionError('review not cancelled')
            assert not terminal.reviewing
    asyncio.run(scenario())


def test_retained_output_inspection_does_not_call_endpoint(workspace):
    from skua.client import ToolCall
    cfg = build_config(workspace, [('test', {'schema_version': 1, 'shell': {'executable': '/bin/bash'},
                       'profiles': {'default': {'base_url': 'https://example/v1', 'model': 'm'}},
                       'output': {'model_max_bytes': 10, 'retain_max_bytes': 100}})])
    answers = iter(['work', 'yes', '/output 3', '/exit', 'discard'])
    output = []
    class UI(Terminal):
        def __init__(self):
            pass
        async def read(self, message=''):
            return next(answers)
        def write(self, text, **kwargs):
            output.append(text)
    class Client:
        requests = 0
        async def complete(self, messages, **kwargs):
            self.requests += 1
            if self.requests == 1:
                return Reply('', (ToolCall('run_command', {'command': 'printf retained-output'}),))
            return Reply('done')
    client = Client()
    state = asyncio.run(conversation(cfg, client, UI()))
    assert client.requests == 2
    assert any('retained-output' in line and 'stdout:' in line for line in output)
    assert len([e for e in state.events if e['type'] == 'tool_call']) == 1


def test_long_review_pager_close_does_not_approve():
    from skua.permissions import Review
    async def scenario():
        with create_pipe_input() as pipe:
            terminal = Terminal(input=pipe, output=DummyOutput(), console=Console(file=StringIO()))
            review = Review.create('patch', ['write'], [], '\n'.join(f'line {i}' for i in range(100)), [])
            task = asyncio.create_task(terminal.approve(review))
            await asyncio.sleep(.05)
            pipe.send_text('q')
            await asyncio.sleep(.05)
            assert not task.done()
            pipe.send_text('yes\r')
            assert await task == 'approved'
    asyncio.run(scenario())
