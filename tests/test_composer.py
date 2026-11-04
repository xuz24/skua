"""Large paste display never substitutes or drops submitted text."""
import asyncio
from types import SimpleNamespace

from prompt_toolkit.input import create_pipe_input, vt100_parser
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput

from skua.composer import PasteBlocks, shifted_enter
from skua.terminal import Terminal


def render(blocks, text):
    transformed = blocks.apply_transformation(SimpleNamespace(fragments=[('', text)]))
    return ''.join(fragment[1] for fragment in transformed.fragments), transformed


def test_paste_mapping_counts_unicode_and_preserves_manual_marker_text():
    blocks = PasteBlocks()
    payload = '猫🙂' * 300
    marker = blocks.paste(payload)
    assert len(marker) == 1
    text = 'before ' + marker + ' after [600 chars]'
    displayed, mapping = render(blocks, text)
    assert displayed == 'before [600 chars] after [600 chars]'
    assert blocks.expand(text) == 'before ' + payload + ' after [600 chars]'
    assert mapping.source_to_display(7) == 7
    assert mapping.source_to_display(8) == 18
    assert mapping.display_to_source(12) == 7
    assert mapping.display_to_source(18) == 8


def test_literal_private_use_characters_cannot_impersonate_paste_markers():
    blocks = PasteBlocks()
    original = blocks.paste('secret draft' * 100)
    literal = blocks.literal(original)
    assert literal != original and blocks.expand(literal) == original
    assert blocks.expand(blocks.paste(original)) == original
    # Expansion is intentionally not recursive, including in a large paste.
    payload = original * 600
    assert blocks.expand(blocks.paste(payload)) == payload


def test_multiline_paste_normalization_and_separate_blocks():
    blocks = PasteBlocks()
    first = blocks.paste('a\r\nb\rc\nd\ne\nf')
    second = blocks.paste('another' * 100)
    assert len(first) == len(second) == 1 and first != second
    assert blocks.expand(first + ' between ' + second) == 'a\nb\nc\nd\ne\nf between ' + 'another' * 100
    assert blocks.paste('small\r\npaste') == 'small\npaste'


def test_large_paste_stays_compact_and_full_text_survives_history():
    async def scenario():
        with create_pipe_input() as pipe:
            terminal = Terminal(input=pipe, output=DummyOutput())
            payload = 'large pasted line 🙂\n' * 60
            task = asyncio.create_task(terminal.read())
            await asyncio.sleep(.03)
            pipe.send_text('before \x1b[200~' + payload + '\x1b[201~ after')
            await asyncio.sleep(.05)
            buffer = terminal.prompt.default_buffer
            assert len(buffer.text) == len('before  after') + 1
            assert render(terminal.paste_blocks, buffer.text)[0] == f'before [{len(payload)} chars] after'
            assert not task.done()
            pipe.send_text('\r')
            assert await task == 'before ' + payload + ' after'
            task = asyncio.create_task(terminal.read())
            await asyncio.sleep(.03)
            pipe.send_text('\x1b[A')
            await asyncio.sleep(.03)
            assert render(terminal.paste_blocks, buffer.text)[0] == f'before [{len(payload)} chars] after'
            pipe.send_text('\r')
            assert await task == 'before ' + payload + ' after'
    asyncio.run(scenario())


def test_delete_undo_and_clear_do_not_submit_hidden_paste():
    async def scenario():
        with create_pipe_input() as pipe:
            terminal = Terminal(input=pipe, output=DummyOutput())
            task = asyncio.create_task(terminal.read())
            await asyncio.sleep(.03)
            pipe.send_text('\x1b[200~' + 'x' * 600 + '\x1b[201~')
            await asyncio.sleep(.03)
            original = terminal.prompt.default_buffer.text
            pipe.send_text('\x7f')
            await asyncio.sleep(.03)
            assert terminal.prompt.default_buffer.text == ''
            pipe.send_text('\x1f')  # Ctrl+_: undo the atomic deletion.
            await asyncio.sleep(.03)
            assert terminal.prompt.default_buffer.text == original
            pipe.send_text('\x03')
            await asyncio.sleep(.03)
            assert terminal.prompt.default_buffer.text == ''
            pipe.send_text('only this\r')
            assert await task == 'only this'
    asyncio.run(scenario())


def test_long_manual_typing_wraps_and_never_collapses():
    async def scenario():
        with create_pipe_input() as pipe:
            terminal = Terminal(input=pipe, output=DummyOutput())
            task = asyncio.create_task(terminal.read())
            await asyncio.sleep(.03)
            text = 'manually typed words ' * 40
            pipe.send_text(text)
            await asyncio.sleep(.1)
            assert terminal.prompt.default_buffer.text == text
            assert not terminal.paste_blocks.blocks
            window = terminal.prompt.layout.current_window
            assert window.wrap_lines()
            height = window.preferred_height(40, 40)
            assert height.preferred > 1 and height.max == 8
            pipe.send_text('\r')
            assert await task == text
    asyncio.run(scenario())


def test_shift_enter_mappings_are_scoped_and_preserve_plain_enter():
    sequence = '\x1b[27;2;13~'
    original = vt100_parser.ANSI_SEQUENCES.get(sequence)
    keys = []
    with shifted_enter():
        parser = vt100_parser.Vt100Parser(keys.append)
        parser.feed_and_flush('\r' + sequence + '\x1b[13;2u')
    assert [key.key for key in keys] == [Keys.ControlM, Keys.ControlJ, Keys.ControlJ]
    assert vt100_parser.ANSI_SEQUENCES.get(sequence) == original
