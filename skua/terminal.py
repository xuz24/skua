"""Scrolling terminal conversation with explicit submission and in-memory history."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import signal
import shlex
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.styles import Style as PromptStyle
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from .activity import Activity
from .context import compact, estimate as estimate_context
from .agent import run_turn
from .client import ChatClient, ModelError, check_capabilities
from .config import Config, ConfigError, resolve_credential
from .session import ModelIdentity, Session
from .storage import Store, StorageError, snapshot, path_for, owns_export
from .tools import Runtime
from .permissions import AccessError
from .startup import maybe_play


class ProjectPathCompleter(Completer):
    """Complete discovered project names only; never open file contents."""
    def __init__(self, root, config=None):
        self.root = root
        self.config = config

    def get_completions(self, document, complete_event):
        from .discovery import discover
        from .permissions import AccessError, Target, sensitive
        before = document.text_before_cursor
        word = before.rsplit(None, 1)[-1] if before else ''
        if not word:
            return
        try:
            paths = discover(self.root)
        except (AccessError, OSError):
            return
        for path in paths:
            try:
                if self.config is not None and sensitive(self.config, path):
                    continue
                target = Target.inspect(path)
                if not target.resolved.is_relative_to(self.root.resolve()):
                    continue
                relative = path.relative_to(self.root).as_posix()
                if relative.startswith(word):
                    yield Completion(relative, start_position=-len(word), display=relative)
            except (AccessError, OSError, ValueError):
                continue


def safe_text(text: str) -> str:
    # Endpoint/user text is text, never terminal control sequences or Rich markup.
    return ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) < 160))


def diff_style(line, limited_colors=False):
    header = '#000080' if limited_colors else '#293443'
    added = '#008000' if limited_colors else '#163b2b'
    removed = '#800000' if limited_colors else '#482329'
    if line.startswith(('diff --git ', '--- ', '+++ ', 'index ')):
        return f'bold #e5e7eb on {header}'
    if line.startswith('@@'):
        return f'#e5e7eb on {header}'
    if line.startswith('+'):
        return f'#e5e7eb on {added}'
    if line.startswith('-'):
        return f'#e5e7eb on {removed}'
    return ''


class ReviewLexer(Lexer):
    def __init__(self, width=lambda: 0, limited_colors=False):
        self.width = width
        self.limited_colors = limited_colors

    def lex_document(self, document):
        def get_line(number):
            line = document.lines[number]
            style = diff_style(line, self.limited_colors)
            if style:
                line += ' ' * max(0, self.width() - get_cwidth(line))
            return [(style.replace(' on ', ' bg:'), line)]
        return get_line


class ActivityDisplay:
    def __init__(self, activity):
        self.activity = activity

    def __rich__(self):
        activity = self.activity
        label = {'tool': 'Running', 'model': 'Waiting for model',
                 'approval': 'Waiting for approval', 'compaction': 'Compacting context'}[activity.phase]
        operation = '' if activity.phase == 'model' else ' · ' + activity.operation
        call = f' #{activity.call_seq}' if activity.call_seq is not None else ''
        return Text(safe_text(f'{label}{operation}{call} · {activity.elapsed:.1f}s · Ctrl-C cancel'))


class Terminal:
    def __init__(self, *, input=None, output=None, console=None, root=None, config=None):
        self.console = console or Console(no_color=False)
        self.root = root
        self.config = config
        self.exit_armed = False
        self.reviewing = False
        self.working = False
        self.status = None
        self.activity = None
        self.context_label = 'context not estimated'
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
                                    color_depth={'truecolor': ColorDepth.DEPTH_24_BIT,
                                                 'standard': ColorDepth.DEPTH_4_BIT,
                                                 'windows': ColorDepth.DEPTH_4_BIT}.get(
                                                     self.console.color_system, ColorDepth.DEPTH_8_BIT),
                                    completer=ProjectPathCompleter(root, config) if root else None,
                                    complete_while_typing=False,
                                    bottom_toolbar=self._toolbar,
                                    refresh_interval=0.2,
                                    style=PromptStyle.from_dict({'prompt': '#d1d5db bold',
                                                           'input-field': 'bg:#202329 #e5e7eb',
                                                           'bottom-toolbar': '#8b949e',
                                                           'completion-menu.completion': 'bg:#1f2937 #f8fafc',
                                                           'completion-menu.completion.current': 'bg:#374151 #ffffff'}))
        input_window = self.prompt.layout.current_window
        input_window.style = lambda: '' if self.prompt.app.is_done else 'class:input-field'
        input_window.height = Dimension.exact(1)
        def reset_exit(_):
            self.exit_armed = False
        self.prompt.default_buffer.on_text_changed += reset_exit
        self.prompt.default_buffer.on_cursor_position_changed += reset_exit

    async def read(self, message='skua> '):
        self.finish_reply()
        self.exit_armed = False
        heading = safe_text(message.strip() or 'skua')
        prefix = '❯ ' if message == 'skua> ' else heading + '\n❯ '
        return await self.prompt.prompt_async(prefix, prompt_continuation='  ')

    def _toolbar(self):
        if self.exit_armed:
            return '  Ctrl-C again to exit'
        activity = getattr(self, 'activity', None)
        if activity and activity.phase == 'approval':
            return '  ' + ActivityDisplay(activity).__rich__().plain + ' · Enter confirm'
        if self.reviewing:
            return '  Enter confirm · Ctrl-C cancel'
        if self.working:
            return '  Thinking… · Ctrl-C cancel'
        context = '' if self.context_label == 'context not estimated' else self.context_label + ' · '
        return '  ' + context + 'Enter send · Alt+Enter newline · Tab paths · /help'

    def show_activity(self, activity):
        status = getattr(self, 'status', None)
        if status is not None:
            status.stop()
            self.status = None
        self.finish_reply()
        self.activity = activity if activity and activity.status is None else None
        if activity is None:
            return
        if activity.status is not None:
            detail = f' · {activity.summary}' if activity.summary else ''
            self.write(f'{activity.operation} #{activity.call_seq}: {activity.status}'
                       f' · {activity.elapsed:.1f}s{detail}')
        elif activity.phase != 'approval' and hasattr(self, 'console'):
            self.status = self.console.status(ActivityDisplay(activity), spinner='dots')
            self.status.start()

    def set_working(self, active):
        self.working = active
        if active:
            self._before_model = getattr(self, 'activity', None)
            self.show_activity(Activity('model', 'model'))
        else:
            self.show_activity(getattr(self, '_before_model', None))
            self._before_model = None

    def write(self, text: str, *, end='\n', style=None):
        self.finish_reply()
        self.console.print(Text(safe_text(text), style=style), end=end, soft_wrap=True)

    def stream_reply(self, text):
        if not hasattr(self, 'console'):
            self.write(text, end='')
            return
        self._reply_text = getattr(self, '_reply_text', '') + safe_text(text)
        rendered = Markdown(self._reply_text)
        if self.console.is_terminal and not self.console.is_dumb_terminal:
            if getattr(self, '_reply_live', None) is None:
                self._reply_live = Live(console=self.console, auto_refresh=False,
                                        transient=True, vertical_overflow='crop')
                self._reply_live.start()
            self._reply_live.update(rendered, refresh=True)

    def finish_reply(self):
        text = getattr(self, '_reply_text', '')
        live = getattr(self, '_reply_live', None)
        self._reply_text = ''
        self._reply_live = None
        if live is not None:
            live.update(Text())
            live.stop()
        if text:
            self.console.print(Markdown(text))

    def write_review(self, text):
        self.finish_reply()
        if not hasattr(self, 'console'):
            self.write(text)
            return
        for line in safe_text(text).splitlines():
            style = diff_style(line, self.console.color_system in {'standard', 'windows'})
            rendered = Text(line, style=style)
            if style:
                for row in rendered.wrap(self.console, self.console.width, overflow='fold'):
                    row.pad_right(max(0, self.console.width - row.cell_len))
                    self.console.print(row, soft_wrap=True)
            else:
                self.console.print(rendered, overflow='fold')

    async def approve(self, review):
        if len(review.summary.splitlines()) > 80:
            await self.pager(review.summary)
        else:
            self.write_review(review.summary)
        reasons = [reason.replace('_', ' ') for reason in review.reasons
                   if reason not in {'read', 'write', 'command', 'execute', 'git_change'}]
        if reasons:
            self.write('Access: ' + ', '.join(reasons))
        if review.targets:
            self.write('Targets: ' + ', '.join(review.targets))
        self.reviewing = True
        try:
            answer = (await self.read('Approve? [y/N]: ')).strip().lower()
            if answer == '/exit':
                return 'cancelled'
            return 'approved' if answer in {'y', 'yes'} else 'denied'
        finally:
            self.reviewing = False

    async def pager(self, text):
        """Full, scrollable diff review; closing never means approval."""
        self.finish_reply()
        from prompt_toolkit.application import Application
        from prompt_toolkit.layout import Layout, HSplit, Window
        from prompt_toolkit.layout.controls import FormattedTextControl
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
                        read_only=True, scrollbar=True, wrap_lines=False,
                        lexer=ReviewLexer(lambda: max(1, self.prompt.app.output.get_size().columns - 1),
                                          self.prompt.color_depth == ColorDepth.DEPTH_4_BIT))
        layout = Layout(HSplit([area, Window(FormattedTextControl(self._toolbar), height=1)]),
                        focused_element=area)
        await Application(layout=layout, key_bindings=keys, full_screen=True, refresh_interval=0.2,
                          color_depth=self.prompt.color_depth,
                          input=self.prompt.app.input, output=self.prompt.app.output).run_async()

    async def continue_tools(self, count):
        previous = getattr(self, 'activity', None)
        self.show_activity(Activity('approval', 'another tool block'))
        self.reviewing = True
        try:
            return (await self.read(f'{count} tool calls attempted. Continue another block? [y/N]: ')).strip().lower() in {'y', 'yes'}
        finally:
            self.reviewing = False
            self.show_activity(previous)

    async def permissions_command(self, session, config, args):
        policy = session.permissions
        try:
            if args == ['/permissions']:
                self.write('\n'.join(g.description for g in policy.grants.values()) or
                           'No session grants. Operations use per-operation review.')
                return
            if len(args) == 3 and args[1] == 'revoke':
                if policy.revoke(args[2]):
                    session.notice('permission_revoked', f'Session grant revoked: {args[2]}')
                    self.write(f'Session grant revoked: {args[2]}')
                else:
                    self.write('No such session grant.')
                return
            if len(args) != 4 or args[1] != 'grant':
                self.write('Usage: /permissions · /permissions grant patch <directory> · '
                           '/permissions grant command "exact command" · /permissions revoke <id|all>')
                return
            grant = policy.prepare(config, args[2], args[3])
            self.write('Grant for this session only: ' + grant.description)
            self.write('Sensitive/external targets and symlink aliases still require review. '
                       'Git actions and recovery remain separately reviewed.')
            if grant.action == 'command':
                self.write('This authorizes the entire exact shell string, including any composition. '
                           'Commands can execute changing project code and affect files anywhere; '
                           'the project directory is the working directory, not a sandbox. '
                           'Detected sensitive/external targets still require review.')
            else:
                self.write('Contents may change between operations within this directory. '
                           'Each operation still checks current file and repository identities.')
            self.reviewing = True
            try:
                answer = (await self.read('Grant session permission? [y/N]: ')).strip().lower()
            except (asyncio.CancelledError, EOFError):
                self.write('Grant cancelled; no permission added.')
                return
            finally:
                self.reviewing = False
            if answer not in {'y', 'yes'}:
                self.write('No permission added.')
                return
            policy.add(grant, config)
            session.notice('permission_granted', 'Session-only grant: ' + grant.description)
            self.write('Granted: ' + grant.description)
        except (AccessError, ConfigError) as exc:
            self.write(str(exc))

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
                    self.write(f'Exported: {destination} (session not saved)')
                else:
                    store.save(session, destination, expected, overwrite=overwrite, name=name)
                    self.write(f'Saved: {destination}')
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


def context_estimate(session, runtime=None) -> int:
    from .agent import SYSTEM_MESSAGE
    from .tools import CATALOG
    payload = [{'role': 'system', 'content': SYSTEM_MESSAGE['content'] +
                (('\nManaged Git: ' + ('enabled' if runtime.config.managed_git else 'disabled; leave edits uncommitted') +
                  '\nDefault notes directory: ' + str(runtime.config.notes_directory)) if runtime else '')},
               *session.active_messages()]
    if runtime and runtime.instructions and runtime.instructions.files:
        payload.append(runtime.instructions.message())
    return estimate_context(payload, CATALOG)


async def conversation(config: Config, client: ChatClient, terminal: Terminal, *, initial=None) -> Session:
    profile = config.profile()
    identity = ModelIdentity(config.selected_profile, profile.base_url, profile.model)
    store = Store(config.root, config.sessions_directory)
    def new_session():
        return Session(config.root, identity)
    session = initial or new_session()
    runtime = Runtime(config, approve=terminal.approve, continue_block=terminal.continue_tools,
                      on_notice=terminal.write, on_activity=terminal.show_activity)
    runtime.client = client
    runtime.on_waiting = terminal.set_working
    terminal.write('/help for commands · History saves only on request.')
    terminal.context_label = f'~{context_estimate(session, runtime):,} tokens estimated'
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
                                   '/undo [revert|local] (default: reversing commit; requires managed Git)\n'
                                   '/compact · /context · /recover · /abandon\n'
                                   '/permissions [grant patch <directory>|grant command "exact command"|revoke <id|all>]\n'
                                   'Per-operation review is the default; session grants are opt-in. Enter approves only with y/yes.')
                    continue
                if command.startswith('/'):
                    try:
                        args = shlex.split(command)
                    except ValueError:
                        terminal.write('Invalid command quoting.')
                        continue
                    if args[0] == '/permissions':
                        await terminal.permissions_command(session, config, args)
                    elif args[0] == '/undo' and (len(args) == 1 or len(args) == 2 and args[1] in {'local', 'revert'}):
                        task = asyncio.create_task(runtime.workflows.undo(session, args[1] if len(args) == 2 else None))
                        with cancel_on_sigint(task):
                            result = await task
                        session.notice('undo', result.text)
                        terminal.write(result.text)
                    elif args == ['/recover']:
                        await runtime.workflows.recovery(session)
                    elif args == ['/context']:
                        estimate = context_estimate(session, runtime)
                        terminal.write(f'Context: ~{estimate:,} tokens (characters ÷ 4; provider usage and limit unknown).')
                        policy = config.context
                        terminal.write(f'Configured budget: {policy.budget_tokens or "not set"}; '
                                       f'automatic compaction: {"on" if policy.auto_compact else "off"}; '
                                       f'threshold: {policy.threshold_percent}%. Budget is an estimate target, not a hard limit.')
                    elif args == ['/compact']:
                        task = asyncio.create_task(compact(session, client, on_activity=terminal.show_activity))
                        try:
                            with cancel_on_sigint(task):
                                await task
                            terminal.write('Context compacted. Full history preserved.')
                            terminal.context_label = f'~{context_estimate(session, runtime):,} tokens estimated'
                        except asyncio.CancelledError:
                            terminal.write('Compaction cancelled; active context is unchanged.')
                        except (ModelError, ValueError, TimeoutError) as exc:
                            terminal.write(f'Compaction failed: {exc}. Active context is unchanged.')
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
                                           on_waiting=terminal.set_working,
                                           on_text=terminal.stream_reply,
                                           on_notice=lambda value: terminal.write('\n' + value)))
                terminal.write('\nAssistant:', style='bold cyan')
                with cancel_on_sigint(task):
                    try:
                        await task
                    except asyncio.CancelledError:
                        terminal.write('Response cancelled.')
                terminal.write('')
                estimate = context_estimate(session, runtime)
                terminal.context_label = f'~{estimate:,} tokens estimated'
            except StorageError as exc:
                terminal.write(str(exc))
            except asyncio.CancelledError:
                terminal.write('Cancelled. Conversation retained.')
            except EOFError:
                terminal.write('Input closed. Unsaved conversation discarded.')
                return session
    finally:
        terminal.show_activity(None)
        terminal.finish_reply()
        session.close()


async def start(config: Config, *, terminal=None, client_factory=ChatClient, resume=None) -> None:
    play_startup = terminal is None
    terminal = terminal or Terminal(root=config.root, config=config)
    session = None
    try:
        if play_startup:
            await maybe_play(config.startup_animation)
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
            terminal.write('Checking model tool support (uses a small API request)…')
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
