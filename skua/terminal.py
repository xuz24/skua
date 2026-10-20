"""Scrolling terminal conversation with explicit submission and in-memory history."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import signal
import shlex
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
from .storage import Store, StorageError, snapshot, path_for, owns_export
from .tools import Runtime
from .permissions import AccessError


def safe_text(text: str) -> str:
    # Endpoint/user text is text, never terminal control sequences or Rich markup.
    return ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) < 160))


class Terminal:
    def __init__(self, *, input=None, output=None, console=None):
        self.console = console or Console()
        self.exit_armed = False
        self.reviewing = False
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
            if self.reviewing:
                event.app.exit(exception=asyncio.CancelledError())
            elif event.current_buffer.text:
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

    async def approve(self, review):
        if len(review.summary.splitlines()) > 80:
            await self.pager(review.summary)
        else:
            self.write(review.summary)
        self.write('Reasons: ' + ', '.join(review.reasons))
        for target in review.targets:
            self.write('Target: ' + target)
        self.reviewing = True
        try:
            answer = (await self.read('Approve this operation? [y/N]: ')).strip().lower()
            if answer == '/exit':
                return 'cancelled'
            return 'approved' if answer in {'y', 'yes'} else 'denied'
        finally:
            self.reviewing = False

    async def pager(self, text):
        """Full, scrollable diff review; closing never means approval."""
        from prompt_toolkit.application import Application
        from prompt_toolkit.layout import Layout
        from prompt_toolkit.widgets import TextArea
        keys = KeyBindings()
        @keys.add('q')
        @keys.add('escape')
        def close(event):
            event.app.exit()
        @keys.add('c-c')
        def cancel(event):
            event.app.exit(exception=asyncio.CancelledError())
        area = TextArea(text='Full review — arrows/PageUp/PageDown scroll; q returns to approval.\n\n' + safe_text(text),
                        read_only=True, scrollbar=True, wrap_lines=False)
        await Application(layout=Layout(area), key_bindings=keys, full_screen=True,
                          input=self.prompt.app.input, output=self.prompt.app.output).run_async()

    async def continue_tools(self, count):
        self.reviewing = True
        try:
            return (await self.read(f'{count} tool calls attempted. Continue another block? [y/N]: ')).strip().lower() in {'y', 'yes'}
        finally:
            self.reviewing = False

    async def save(self, session: Session, store: Store, path=None, *, name=None, export=False) -> bool:
        destination = (path_for(store.root, path) if path is not None else
                       (store.directory / f'{session.session_id}.md' if export else store.destination(session)))
        while True:
            try:
                expected = snapshot(destination)
                own = owns_export(session, destination, expected) if export else (
                    destination == session.destination and expected == session.saved_bytes)
                overwrite = False
                if expected is not None and not own:
                    self.write(f'Destination exists or changed: {destination}')
                    overwrite = (await self.read('Replace this file? [y/N]: ')).strip().lower() in {'y', 'yes'}
                    if not overwrite:
                        return False
                if export:
                    store.export(session, destination, expected, overwrite=overwrite)
                    self.write(f'Exported Markdown: {destination}. JSONL save state is unchanged.')
                else:
                    store.save(session, destination, expected, overwrite=overwrite, name=name)
                    self.write(f'Saved JSONL: {destination}')
                return True
            except StorageError as exc:
                self.write(str(exc))
                answer = (await self.read('Retry / Save elsewhere / Cancel [Cancel]: ')).strip().lower()
                if answer in {'retry', 'r'}:
                    continue
                if answer in {'save elsewhere', 'elsewhere', 'e'}:
                    value = await self.read('Destination path (blank cancels): ')
                    if value.strip() and value != '/exit':
                        destination = path_for(store.root, value.strip())
                        continue
                return False

    async def confirm_transition(self, session: Session, store: Store) -> bool:
        if not session.dirty:
            return True
        self.write('Unsaved conversation.')
        answer = (await self.read('Save / Discard / Cancel [Cancel]: ')).strip().lower()
        if answer in {'save', 's'}:
            return await self.save(session, store)
        return answer in {'discard', 'd'}

    async def confirm_model(self, session: Session, identity: ModelIdentity) -> bool:
        if session.current_model == identity:
            return True
        previous = session.current_model
        self.write(f'Saved model: {previous.profile} / {previous.model} at {previous.base_url}')
        self.write(f'Selected model: {identity.profile} / {identity.model} at {identity.base_url}')
        answer = (await self.read('Continue with the selected model/endpoint? [y/N]: ')).strip().lower()
        if answer not in {'y', 'yes'}:
            return False
        session.change_model(identity)
        return True


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


async def conversation(config: Config, client: ChatClient, terminal: Terminal, *, initial=None) -> Session:
    profile = config.profile()
    identity = ModelIdentity(config.selected_profile, profile.base_url, profile.model)
    store = Store(config.root, config.sessions_directory)
    def new_session():
        return Session(config.root, identity)
    session = initial or new_session()
    runtime = Runtime(config, approve=terminal.approve, continue_block=terminal.continue_tools,
                      on_notice=terminal.write)
    terminal.write('History saves only on request. /help lists commands. Read tools are available; shell commands require approval.')
    try:
        while True:
            try:
                text = await terminal.read()
                if not text.strip():
                    continue
                command = text.strip()
                if command in {'/exit', '/new'}:
                    if await terminal.confirm_transition(session, store):
                        if command == '/exit':
                            return session
                        session.close()
                        session = new_session()
                        runtime.workflows.pending = None
                        runtime.workflows.init_offered = False
                    continue
                if command == '/help':
                    terminal.write('Enter: send · Alt+Enter: newline · Ctrl-C: cancel/clear\n'
                                   '/save [path] [--name "name"] · /export [path.md] · /sessions\n'
                                   '/resume <id-or-path> · /output <call-seq> · /new · /exit\n'
                                   '/undo [local|revert] · /recover · /abandon\n'
                                   'Sensitive/external reads and shell commands require approval. Enter approves only with y/yes.')
                    continue
                if command.startswith('/'):
                    try:
                        args = shlex.split(command)
                    except ValueError:
                        terminal.write('Invalid command quoting.')
                        continue
                    if args[0] == '/undo' and (len(args) == 1 or len(args) == 2 and args[1] in {'local', 'revert'}):
                        task = asyncio.create_task(runtime.workflows.undo(session, args[1] if len(args) == 2 else None))
                        with cancel_on_sigint(task):
                            result = await task
                        session.notice('undo', result.text)
                        terminal.write(result.text)
                    elif args == ['/recover']:
                        await runtime.workflows.recovery(session)
                    elif args == ['/abandon']:
                        runtime.workflows.pending = None
                        session.notice('change_abandoned', 'Pending lint grouping cleared; files remain unchanged and uncommitted.')
                        terminal.write('Pending lint grouping cleared; files remain unchanged and uncommitted.')
                    elif args[0] == '/output' and len(args) == 2:
                        found = next((e for e in session.events if e['type'] == 'tool_result'
                                      and str(e['call_seq']) == args[1] and e['output']), None)
                        if found is None:
                            terminal.write('No retained command output for that call.')
                        else:
                            for chunk in found['output']['chunks']:
                                if 'omitted_bytes' in chunk:
                                    terminal.write(f"[{chunk['omitted_bytes']} bytes discarded]")
                                else:
                                    terminal.write(f"{chunk['stream']}:\n{chunk['text']}")
                    elif args[0] == '/sessions' and len(args) == 1:
                        terminal.write('\n'.join(store.list_sessions()) or 'No saved sessions.')
                    elif args[0] in {'/save', '/export'}:
                        name = None
                        values = args[1:]
                        if args[0] == '/save' and '--name' in values:
                            index = values.index('--name')
                            if index + 1 >= len(values):
                                terminal.write('Usage: /save [path] [--name "name"]')
                                continue
                            name = values[index + 1]
                            del values[index:index + 2]
                        if len(values) > 1:
                            terminal.write('Expected at most one destination path; quote paths containing spaces.')
                            continue
                        await terminal.save(session, store, values[0] if values else None,
                                            name=name, export=args[0] == '/export')
                    elif args[0] == '/resume' and len(args) == 2:
                        candidate = store.load(args[1])
                        try:
                            if not await terminal.confirm_model(candidate, identity):
                                continue
                            if not await terminal.confirm_transition(session, store):
                                continue
                            session.close()
                            session = candidate
                            runtime.workflows.pending = None
                            runtime.workflows.init_offered = False
                            candidate = None
                            terminal.write(f'Resumed session {session.session_id}.')
                        finally:
                            if candidate is not None:
                                candidate.close()
                    else:
                        terminal.write('Unknown or unavailable command. Use /help.')
                    continue
                task = asyncio.create_task(run_turn(session, client, text,
                                           runtime=runtime,
                                           on_text=lambda value: terminal.write(value, end=''),
                                           on_notice=lambda value: terminal.write('\n' + value)))
                with cancel_on_sigint(task):
                    try:
                        await task
                    except asyncio.CancelledError:
                        terminal.write('Response cancelled. Returned to the prompt.')
                terminal.write('')
            except StorageError as exc:
                terminal.write(str(exc))
            except asyncio.CancelledError:
                terminal.write('Operation cancelled; the current conversation is retained.')
            except EOFError:
                terminal.write('Input closed. Unsaved changes were not saved; existing saves are unchanged.')
                return session
    finally:
        session.close()


async def start(config: Config, *, terminal=None, client_factory=ChatClient, resume=None) -> None:
    terminal = terminal or Terminal()
    session = None
    try:
        if resume is not None:
            session = Store(config.root, config.sessions_directory).load(resume)
            profile = config.profile()
            identity = ModelIdentity(config.selected_profile, profile.base_url, profile.model)
            if not await terminal.confirm_model(session, identity):
                return
        recovery_directory = config.root / '.skua/.tmp'
        if recovery_directory.exists():
            if session is None:
                profile = config.profile()
                session = Session(config.root, ModelIdentity(config.selected_profile, profile.base_url, profile.model))
            recovery = Runtime(config, approve=terminal.approve, on_notice=terminal.write)
            try:
                await recovery.workflows.recovery(session)
            except AccessError as exc:
                terminal.write(str(exc))
        credential = resolve_credential(config)
        async with client_factory(config.profile(), credential) as client:
            terminal.write('Checking endpoint/native tool support with a synthetic request; this may consume tokens. No tool is executed.')
            probe = asyncio.create_task(check_capabilities(client, on_retry=lambda n, d: terminal.write(f'Diagnostic retry {n}/3 in {d:g}s.')))
            with cancel_on_sigint(probe):
                await probe
            await conversation(config, client, terminal, initial=session)
    finally:
        if session is not None:
            session.close()


def run_chat(config: Config, *, resume=None) -> int:
    config.profile()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ConfigError('interactive chat requires a terminal; use skua squak for endpoint diagnostics')
    try:
        asyncio.run(start(config, resume=resume))
    except asyncio.CancelledError:
        return 130
    return 0
