"""Scrolling terminal conversation with explicit submission and in-memory history."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import signal
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from rich.console import Console
from rich.text import Text

from .agent import run_turn
from .client import ChatClient, check_capabilities
from .config import Config, ConfigError, resolve_credential
from .session import ModelIdentity, Session


def safe_text(text: str) -> str:
    # Endpoint/user text is text, never terminal control sequences or Rich markup.
    return ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) < 160))


class Terminal:
    def __init__(self, *, input=None, output=None, console=None):
        self.console = console or Console()
        self.exit_armed = False
        bindings = KeyBindings()

        @bindings.add('enter')
        def submit(event):
            self.exit_armed = False
            event.current_buffer.validate_and_handle()

        @bindings.add('escape', 'enter')
        def newline(event):
            self.exit_armed = False
            event.current_buffer.insert_text('\n')

        @bindings.add('c-c')
        def interrupt(event):
            if event.current_buffer.text:
                event.current_buffer.reset()
                self.exit_armed = False
            elif self.exit_armed and [key.key for key in event.previous_key_sequence] == [Keys.ControlC]:
                self.exit_armed = False
                event.app.exit(result='/exit')
            else:
                self.exit_armed = True

        @bindings.add('c-d')
        def eof(event):
            self.exit_armed = False
            if not event.current_buffer.text:
                event.app.exit(result='/exit')
            else:
                event.current_buffer.delete()

        self.prompt = PromptSession(multiline=True, key_bindings=bindings, input=input, output=output,
                                    bottom_toolbar=lambda: 'Press Ctrl-C again to exit' if self.exit_armed else '')
        def reset_exit(_):
            self.exit_armed = False
        self.prompt.default_buffer.on_text_changed += reset_exit
        self.prompt.default_buffer.on_cursor_position_changed += reset_exit

    async def read(self, message='skua> '):
        self.exit_armed = False
        return await self.prompt.prompt_async(message)

    def write(self, text: str, *, end='\n'):
        self.console.print(Text(safe_text(text)), end=end, soft_wrap=True)

    async def confirm_discard(self, session: Session) -> bool:
        if not session.events:
            return True
        self.write('Unsaved conversation. Save is unavailable until milestone 4.')
        answer = (await self.read('Discard / Cancel [Cancel]: ')).strip().lower()
        return answer in {'discard', 'd'}


@contextmanager
def cancel_on_sigint(task):
    """Ctrl-C cancels active work; it does not cancel the surrounding conversation."""
    loop = asyncio.get_running_loop()
    old = signal.getsignal(signal.SIGINT)
    def cancel_once():
        if not task.cancelling():
            task.cancel()
    loop.add_signal_handler(signal.SIGINT, cancel_once)
    try:
        yield
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        signal.signal(signal.SIGINT, old)


async def conversation(config: Config, client: ChatClient, terminal: Terminal) -> Session:
    profile = config.profile()
    def new_session():
        return Session(config.root, ModelIdentity(config.selected_profile, profile.base_url, profile.model))
    session = new_session()
    terminal.write('Conversation is held in memory only. Tools and saving are unavailable. /help lists commands.')
    while True:
        try:
            text = await terminal.read()
            if not text.strip():
                continue
            command = text.strip()
            if command in {'/exit', '/new'}:
                if await terminal.confirm_discard(session):
                    if command == '/exit':
                        return session
                    session = new_session()
                continue
            if command == '/help':
                terminal.write('Enter: send · Alt+Enter: newline · Ctrl-C: cancel/clear\n/new: discard-confirmed new conversation · /exit: discard-confirmed exit\nSaving, resume, file access, and shell tools are not available yet.')
                continue
            if command.startswith('/'):
                terminal.write('Unknown or unavailable command. Use /help.')
                continue
            task = asyncio.create_task(run_turn(session, client, text,
                                       on_text=lambda value: terminal.write(value, end=''),
                                       on_notice=lambda value: terminal.write('\n' + value)))
            with cancel_on_sigint(task):
                try:
                    await task
                except asyncio.CancelledError:
                    # A signal can arrive before the new task has entered run_turn.
                    terminal.write('Response cancelled. Returned to the prompt.')
            terminal.write('')
        except EOFError:
            terminal.write('Input closed. In-memory history was not saved.')
            return session


async def start(config: Config, *, terminal=None, client_factory=ChatClient) -> None:
    terminal = terminal or Terminal()
    credential = resolve_credential(config)
    async with client_factory(config.profile(), credential) as client:
        terminal.write('Checking endpoint/native tool support with a synthetic request; this may consume tokens. No tool is executed.')
        probe = asyncio.create_task(check_capabilities(client, on_retry=lambda n, d: terminal.write(f'Diagnostic retry {n}/3 in {d:g}s.')))
        with cancel_on_sigint(probe):
            await probe
        await conversation(config, client, terminal)


def run_chat(config: Config) -> int:
    config.profile()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ConfigError('interactive chat requires a terminal; use skua squak for endpoint diagnostics')
    try:
        asyncio.run(start(config))
    except asyncio.CancelledError:
        return 130
    return 0
