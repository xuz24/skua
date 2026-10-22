"""Offline installed-package acceptance workflow. No endpoint or user files are used."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from unittest.mock import patch

import httpx
import skua
from skua.cli import main as cli
from skua.client import ChatClient
from skua.config import load_config
from skua.git import Repo
from skua.setup import run_setup
from skua.squak import full_checks
from skua.storage import Store
from skua.startup import _asset
from skua.terminal import Terminal, start


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True, capture_output=True).stdout.decode().strip()


class ScriptedTerminal(Terminal):
    def __init__(self, answers):
        self.answers = iter(answers)
        self.output = []
        self.prompts = []

    async def read(self, message='skua> '):
        self.prompts.append(message)
        try:
            expected, answer = next(self.answers)
        except StopIteration:
            raise AssertionError('Unexpected prompt: ' + message) from None
        assert expected in message, (expected, message)
        return answer

    def write(self, text, **kwargs):
        self.output.append(text)

    def exhausted(self):
        assert next(self.answers, None) is None, 'Not all scripted inputs were consumed'


def wire_reply(text='', calls=(), *, streaming=False):
    tools = [{'id': f'remote_{i}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}
             for i, (name, args) in enumerate(calls)]
    finish = 'tool_calls' if calls else 'stop'
    if not streaming:
        return httpx.Response(200, json={'choices': [{'index': 0, 'finish_reason': finish,
                              'message': {'role': 'assistant', 'content': text, **({'tool_calls': tools} if tools else {})}}]})
    delta = {'role': 'assistant', 'content': text}
    if tools:
        delta['tool_calls'] = [dict(tool, index=i) for i, tool in enumerate(tools)]
    events = [{'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
              {'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}]
    body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events) + 'data: [DONE]\n\n'
    return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=body.encode())


async def workflow(root, *, shell='/bin/bash', streaming=True):
    """Caller supplies an isolated HOME and an empty disposable project."""
    requests = []
    def ask(prompt):
        options = {'API base URL': 'https://mock.invalid/v1', 'Model identifier': 'mock-model',
                   'Stream responses': 'yes' if streaming else 'no', 'Use API-key': 'no',
                   'Bash/Zsh executable': shell, 'Load interactive': 'no',
                   'Enable a project lint': 'yes', 'Lint command': 'test -f a.txt',
                   'Save this configuration?': 'yes'}
        return next((value for label, value in options.items() if prompt.startswith(label)), '')
    assert run_setup(root, ask=ask, emit=lambda _: None)
    config = load_config(root)
    assert config.profile().stream is streaming and not config.profile().auth.enabled
    assert cli(['--root', str(root), 'config', 'check']) == 0
    assert cli(['--root', str(root), 'profiles']) == 0
    assert not (root / '.skua').exists()
    git(root, 'init')
    git(root, 'config', 'user.name', 'Skua Acceptance')
    git(root, 'config', 'user.email', 'acceptance@example.invalid')
    (root / '.gitignore').write_text('.skua/\nexports/\n')
    (root / 'a.txt').write_text('original\n')
    (root / 'b.txt').write_text('unrelated\n')
    (root / 'AGENTS.md').write_text('Keep edits scoped to the requested file.\n')
    (root / '.env.example').write_text('PRIVATE_FIXTURE=never-send-this-value\n')
    git(root, 'add', '.')
    git(root, 'commit', '-m', 'user baseline')
    baseline = git(root, 'rev-parse', 'HEAD')
    (root / 'b.txt').write_text('staged unrelated\n')
    git(root, 'add', 'b.txt')
    (root / 'b.txt').write_text('unstaged unrelated\n')

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert 'never-send-this-value' not in str(payload)
        names = [tool['function']['name'] for tool in payload.get('tools', [])]
        if names == ['skua_diagnostic']:
            return wire_reply(calls=[('skua_diagnostic', {'echo': 'skua'})], streaming=streaming)
        messages = payload['messages']
        if any('Write one concise imperative Git commit subject' in m.get('content', '')
               for m in messages if m['role'] == 'system'):
            return wire_reply('Describe reviewed file update', streaming=streaming)
        last_user = max(i for i, m in enumerate(messages) if m['role'] == 'user')
        prompt = messages[last_user]['content']
        results = [m for m in messages[last_user + 1:] if m['role'] == 'tool']
        if not results:
            if prompt == 'inspect and change':
                calls = [('list_files', {'path': '.'}), ('read_file', {'path': 'a.txt'}),
                         ('search_files', {'path': 'a.txt', 'query': 'original'}),
                         ('run_command', {'command': 'printf acceptance-probe'})]
            elif prompt == 'deny sensitive':
                calls = [('read_file', {'path': '.env.example'})]
            else:
                assert prompt == 'failing command'
                calls = [('run_command', {'command': 'printf expected-failure >&2; exit 7'})]
            return wire_reply(calls=calls, streaming=streaming)
        if prompt == 'inspect and change' and len(results) < 6:
            content = 'first\n' if len(results) == 4 else 'second\n'
            return wire_reply(calls=[('apply_patch', {'message': 'Acceptance change',
                              'changes': [{'op': 'write', 'path': 'a.txt', 'content': content}]})], streaming=streaming)
        if prompt == 'deny sensitive':
            assert 'denied' in results[-1]['content'] and 'tools' not in payload
        if prompt == 'failing command':
            assert 'exit code 7' in results[-1]['content']
        return wire_reply('Workflow step complete.', streaming=streaming)

    factory = lambda p, k: ChatClient(p, k, transport=httpx.MockTransport(handler))
    assert await full_checks(config, emit=lambda _: None, client_factory=factory) == 0
    save_path = root / '.skua/sessions/acceptance.jsonl'
    ui = ScriptedTerminal([('skua>', 'inspect and change'),
                           ('Approve', 'yes'),  # Read-only command.
                           ('Approve', 'yes'), ('Approve', 'yes'), ('Approve', 'yes'),  # Patch, lint, commit.
                           ('Approve', 'yes'), ('Approve', 'yes'), ('Approve', 'yes'),  # Correction, lint, commit.
                           ('skua>', f'/save "{save_path}"'), ('skua>', '/exit')])
    await start(config, terminal=ui, client_factory=factory)
    ui.exhausted()
    assert (root / 'a.txt').read_text() == 'second\n'
    assert git(root, 'rev-list', '--count', baseline + '..HEAD') == '2'
    assert git(root, 'show', ':b.txt') == 'staged unrelated'
    assert (root / 'b.txt').read_text() == 'unstaged unrelated\n'
    store = Store(root, config.sessions_directory)
    saved = store.load(str(save_path))
    command_seq = next(e['seq'] for e in saved.events if e['type'] == 'tool_call' and e['name'] == 'run_command')
    saved.close()
    export_path = root / 'exports/acceptance.md'
    export_path.parent.mkdir()
    resumed = ScriptedTerminal([
        ('skua>', f'/output {command_seq}'),
        ('skua>', 'deny sensitive'), ('Approve', 'no'),
        ('skua>', 'failing command'), ('Approve', 'yes'),
        ('skua>', f'/export "{export_path}"'),
        ('skua>', '/undo revert'), ('Approve', 'no'), ('Approve', 'yes'),
        ('skua>', '/undo revert'), ('Approve', 'no'), ('Approve', 'yes'),
        ('skua>', '/save'), ('skua>', '/exit')])
    await start(config, terminal=resumed, client_factory=factory, resume=str(save_path))
    resumed.exhausted()
    assert any('acceptance-probe' in output for output in resumed.output)
    assert export_path.is_file() and '<details>' in export_path.read_text()
    assert (root / 'a.txt').read_text() == 'original\n'
    assert git(root, 'show', ':b.txt') == 'staged unrelated'
    assert (root / 'b.txt').read_text() == 'unstaged unrelated\n'
    repo = Repo.find(root)
    assert repo.undo_target() is None
    assert repo.metadata(repo.head())['type'] == 'undo'
    final = store.load(str(save_path))
    assert any(e['type'] == 'tool_result' and e['status'] == 'denied' for e in final.events)
    assert any(e['type'] == 'tool_result' and e['status'] == 'error' for e in final.events)
    assert not final.dirty
    final.close()
    assert 'never-send-this-value' not in save_path.read_text()
    assert cli(['--root', str(root), 'sessions']) == 0
    return len(requests)


def blocked(*args, **kwargs):
    raise AssertionError('Acceptance workflow attempted a real network connection')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--shell', default='/bin/bash')
    parser.add_argument('--nonstreaming', action='store_true')
    args = parser.parse_args()
    assert 'site-packages' in str(Path(skua.__file__).resolve()), 'Run using an installed wheel, outside the checkout.'
    assert len(_asset()[1]) == 7, 'The startup animation asset is missing from the installed wheel.'
    with tempfile.TemporaryDirectory(prefix='skua-acceptance-') as directory:
        home = Path(directory) / 'home'
        root = Path(directory) / 'project'
        home.mkdir()
        root.mkdir()
        with patch.dict(os.environ, {'HOME': str(home), 'SHELL': args.shell}), \
                patch.object(socket.socket, 'connect', blocked), patch.object(socket, 'create_connection', blocked):
            count = asyncio.run(workflow(root.resolve(), shell=args.shell, streaming=not args.nonstreaming))
    print(f'Installed workflow passed ({count} mocked endpoint requests; network blocked).')


if __name__ == '__main__':
    main()
