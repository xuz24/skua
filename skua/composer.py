"""Atomic, display-only paste blocks for the prompt-toolkit input buffer."""
from contextlib import contextmanager

from prompt_toolkit.input import vt100_parser
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.processors import Processor, Transformation


PASTE_CHAR_LIMIT = 500
PASTE_LINE_LIMIT = 6


@contextmanager
def shifted_enter():
    """Recognize CSI-u and xterm Shift+Enter without changing terminal modes."""
    sequences = ('\x1b[13;2u', '\x1b[13;2~', '\x1b[27;2;13~')
    previous = {sequence: vt100_parser.ANSI_SEQUENCES.get(sequence) for sequence in sequences}
    vt100_parser.ANSI_SEQUENCES.update(dict.fromkeys(sequences, Keys.ControlJ))
    vt100_parser._IS_PREFIX_OF_LONGER_MATCH_CACHE.clear()
    try:
        yield
    finally:
        for sequence, value in previous.items():
            if value is None:
                vt100_parser.ANSI_SEQUENCES.pop(sequence, None)
            else:
                vt100_parser.ANSI_SEQUENCES[sequence] = value
        vt100_parser._IS_PREFIX_OF_LONGER_MATCH_CACHE.clear()


class PasteBlocks(Processor):
    """One buffer character per block, so deletion, selection and undo are atomic.

    Blocks stay in memory with prompt history. Only markers still present in the
    submitted buffer are expanded, once, before returning the user's input.
    Literal private-use characters are escaped to avoid marker impersonation.
    """
    def __init__(self):
        self.blocks = {}
        self.next_marker = 0xF0000

    def add(self, text, *, collapsed=True):
        if self.next_marker == 0xFFFFE:
            self.next_marker = 0x100000
        if self.next_marker > 0x10FFFD:
            raise ValueError('Input history contains too many paste blocks; restart Skua.')
        marker = chr(self.next_marker)
        self.next_marker += 1
        self.blocks[marker] = (text, f'[{len(text)} chars]' if collapsed else text)
        return marker

    def literal(self, text):
        return ''.join(self.add(char, collapsed=False) if 0xF0000 <= ord(char) <= 0x10FFFD else char
                       for char in text)

    def paste(self, text):
        text = text.replace('\r\n', '\n').replace('\r', '\n')
        if len(text) >= PASTE_CHAR_LIMIT or text.count('\n') + 1 >= PASTE_LINE_LIMIT:
            return self.add(text)
        return self.literal(text)

    def expand(self, text):
        return ''.join(self.blocks[char][0] if char in self.blocks else char for char in text)

    def apply_transformation(self, ti):
        fragments = []
        source_to_display = [0]
        display_to_source = []
        source = 0
        for style, text, *extra in ti.fragments:
            for char in text:
                block = self.blocks.get(char)
                display = block[1] if block else char
                fragments.append((style + (' class:paste-block' if block else ''), display, *extra))
                display_to_source.extend([source] * len(display))
                source_to_display.append(source_to_display[-1] + len(display))
                source += 1
        display_to_source.append(source)
        return Transformation(fragments,
            source_to_display=lambda position: source_to_display[min(position, source)],
            display_to_source=lambda position: display_to_source[min(position, len(display_to_source) - 1)])
