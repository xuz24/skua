"""M5 offline edit/test/fix comparison and optional hands-on terminal walkthrough.

Run with the development interpreter; --interactive uses real terminal prompts.
All project changes are confined to temporary directories. No provider is contacted.
"""
import argparse
import asyncio
from io import StringIO
import json
import os
from pathlib import Path
import socket
import tempfile
from unittest.mock import patch

import httpx
from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from installed_smoke import blocked, git, wire_reply
from skua.client import ChatClient
from skua.config import build_config
from skua.context import SUMMARY_PROMPT
from skua.session import ModelIdentity, Session
from skua.storage import decode, encoded
from skua.terminal import Terminal, conversation

LINT = 'sleep 0.3; grep -qx fixed src/a.txt'


class DelayedStream(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        for event in self.body.split(b'\n\n'):
            if event:
                await asyncio.sleep(.08)
                yield event + b'\n\n'


def reply(text='', calls=(), *, streaming):
    response = wire_reply(text, calls, streaming=streaming)
    if not streaming:
        return response
    # Separate text deltas make the walkthrough exercise incremental rendering.
    events = []
    for word in text.split(' '):
        if word:
            events.append('data: ' + json.dumps({'choices': [{'index': 0,
                'delta': {'content': word + ' '}, 'finish_reason': None}]}))
    tail = wire_reply('', calls, streaming=True).content
    body = ('\n\n'.join(events) + '\n\n').encode() + tail
    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=DelayedStream(body))


class AcceptanceTerminal(Terminal):
    def __init__(self, *, interactive=False, smoother=False):
        self.interactive = interactive
        self.reviews = []
        self.grant_prompts = 0
        self.activities = []
        self.capture = StringIO()
        options = {} if interactive else dict(input=DummyInput(), output=DummyOutput(),
                    console=Console(file=self.capture, force_terminal=True, width=100))
        super().__init__(**options)
        self.inputs = iter(([
            '/permissions grant patch src',
            f'/permissions grant command "{LINT}"',
        ] if smoother else []) + ['edit/test/fix', '/context', '/exit'])

    async def read(self, message='skua> '):
        if message.startswith('Grant session permission?'):
            self.grant_prompts += 1
        if self.interactive:
            return await super().read(message)
        self.finish_reply()
        if message == 'skua> ':
            return next(self.inputs)
        if message.startswith(('Approve', 'Grant session permission?')):
            return 'yes'
        if 'discard' in message.lower():
            return 'discard'
        raise AssertionError(f'Unexpected prompt: {message}')

    async def approve(self, review):
        self.reviews.append(review.action)
        return await super().approve(review)

    def show_activity(self, event):
        self.activities.append(event)
        super().show_activity(event)


async def workflow(root, *, smoother=False, shell='/bin/bash', streaming=True, interactive=False):
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    (root / 'src').mkdir()
    (root / 'src/a.txt').write_text('original\n')
    (root / 'unrelated.txt').write_text('original\n')
    (root / '.gitignore').write_text('.skua/\n')
    git(root, 'init')
    git(root, 'config', 'user.name', 'Skua Acceptance')
    git(root, 'config', 'user.email', 'acceptance@example.invalid')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'baseline')
    baseline = git(root, 'rev-parse', 'HEAD')
    (root / 'unrelated.txt').write_text('staged\n')
    git(root, 'add', 'unrelated.txt')
    (root / 'unrelated.txt').write_text('unstaged\n')
    config = build_config(root, [('acceptance', {
        'schema_version': 1,
        'profiles': {'default': {'base_url': 'https://mock.invalid/v1', 'model': 'mock',
                                'stream': streaming, 'auth': {'enabled': False}}},
        'shell': {'executable': shell},
        'git': {'managed': not smoother},
        'lint': {'enabled': True, 'command': LINT},
        'context': {'auto_compact': smoother, 'budget_tokens': 100, 'threshold_percent': 80},
    })])
    session = Session(root, ModelIdentity('default', 'https://mock.invalid/v1', 'mock'))
    for i in range(5):
        session.begin(f'Previous task {i}: ' + 'fixture context ' * 200)
        session.finish('Completed earlier work. ' * 200)
    original = list(session.events)
    requests = []
    async def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        messages = payload['messages']
        system = messages[0]['content']
        if system == SUMMARY_PROMPT:
            return reply('Five earlier fixture tasks completed.', streaming=streaming)
        if 'Write one concise imperative Git commit subject' in system:
            return reply('Fix acceptance fixture', streaming=streaming)
        last_user = max(i for i, m in enumerate(messages) if m['role'] == 'user')
        results = [m for m in messages[last_user + 1:] if m['role'] == 'tool']
        await asyncio.sleep(.05)
        if len(results) < 2:
            if results:
                assert 'error' in results[-1]['content']
            return reply('Applying the fix.' if results else 'Editing the fixture, then running lint.',
                calls=[('apply_patch', {'message': 'Fix fixture', 'changes': [
                    {'op': 'write', 'path': 'src/a.txt', 'content': 'fixed\n' if results else 'broken\n'}]})],
                streaming=streaming)
        assert len(results) == 2 and 'Patch applied' in results[-1]['content'] and 'lint did not pass' not in results[-1]['content'], results
        return reply('**Done.** Lint passed after the correction.', streaming=streaming)

    ui = AcceptanceTerminal(interactive=interactive, smoother=smoother)
    if interactive:
        ui.write(f'Mode: {"smoother" if smoother else "default"}. Disposable project: {root}')
        ui.write('Enter these commands in order; approve each offered review:')
        if smoother:
            ui.write('/permissions grant patch src')
            ui.write(f'/permissions grant command "{LINT}"')
        ui.write('edit/test/fix\n/context\n/exit\nChoose discard at exit. Do not save this fixture.')
    async with ChatClient(config.profile(), None, transport=httpx.MockTransport(handler)) as client:
        await conversation(config, client, ui, initial=session)
    assert (root / 'src/a.txt').read_text() == 'fixed\n', ui.capture.getvalue()
    assert git(root, 'show', ':unrelated.txt') == 'staged'
    assert (root / 'unrelated.txt').read_text() == 'unstaged\n'
    commits = int(git(root, 'rev-list', '--count', baseline + '..HEAD'))
    assert commits == (0 if smoother else 1)
    assert not list((root / '.skua').glob('sessions/*'))
    assert session.events[:len(original)] == original
    assert bool(session.compaction) is smoother
    assert sum(p['messages'][0]['content'] == SUMMARY_PROMPT for p in requests) == int(smoother)
    results = [e for e in session.events if e['type'] == 'tool_result']
    assert [e['status'] for e in results] == ['error', 'ok']
    assert all(e['output'] is not None for e in results)
    assert any(e and e.phase == 'tool' for e in ui.activities)
    assert ui.status is None and ui.activity is None
    if not interactive:
        rendered = ui.capture.getvalue()
        assert 'Running' in rendered
        assert 'Lint passed after the correction.' in rendered
        for result in results:
            assert f"/output {result['call_seq']}" in rendered
        if not smoother:
            assert '-broken' in rendered and '+fixed' in rendered
    decode(encoded(session, None), root)
    session.close()
    expected = [] if smoother else ['patch', 'command', 'patch', 'command', 'commit']
    assert ui.reviews == expected, ui.reviews
    assert ui.grant_prompts == (2 if smoother else 0)
    return {'mode': 'smoother' if smoother else 'default', 'operation_reviews': len(ui.reviews),
            'grant_confirmations': ui.grant_prompts, 'total_authorization_prompts': len(ui.reviews) + ui.grant_prompts,
            'commits': commits, 'summaries': int(smoother), 'transcript_saves': 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interactive', action='store_true')
    parser.add_argument('--smoother', action='store_true', help='Select smoother mode for the interactive walkthrough')
    parser.add_argument('--shell', default='/bin/bash')
    parser.add_argument('--nonstreaming', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='skua-m5-') as directory:
        base = Path(directory)
        home = base / 'home'
        home.mkdir()
        with patch.dict(os.environ, {'HOME': str(home)}), \
                patch.object(socket.socket, 'connect', blocked), patch.object(socket, 'create_connection', blocked):
            for smoother in ([args.smoother] if args.interactive else [False, True]):
                report = asyncio.run(workflow(base / str(smoother), smoother=smoother, shell=args.shell,
                    streaming=not args.nonstreaming, interactive=args.interactive))
                print(json.dumps(report))


if __name__ == '__main__':
    main()
