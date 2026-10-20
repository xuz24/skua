import asyncio
from pathlib import Path

import pytest

from skua.client import Reply
from skua.config import build_config
from skua.session import ModelIdentity, Session
from skua.storage import Store, StorageError, snapshot
from skua.terminal import Terminal, conversation, start
from test_storage import state, save, store


def config(root, model='model'):
    return build_config(root, [('test', {'schema_version':1,'profiles':{'default':{
        'base_url':'https://example/v1','model':model,'auth':{'enabled':False}}}})])


class UI(Terminal):
    def __init__(self, answers):
        self.answers = iter(answers)
        self.output = []
        self.prompts = []
    async def read(self, message='skua> '):
        self.prompts.append(message)
        answer = next(self.answers)
        return answer() if callable(answer) else answer
    def write(self, text, **kwargs):
        self.output.append(text)


class Client:
    def __init__(self):
        self.requests = []
    async def complete(self, messages, **kwargs):
        self.requests.append(messages)
        kwargs['on_text']('answer')
        return Reply('answer')


def test_save_exit_and_clean_exit_no_second_prompt(workspace):
    ui = UI(['hello', '/save --name "My conversation"', '/exit'])
    session = asyncio.run(conversation(config(workspace), Client(), ui))
    assert session.name == 'My conversation' and not session.dirty
    assert session.destination.exists() and session.lock is None
    assert not any('Save / Discard' in prompt for prompt in ui.prompts)
    assert not list(workspace.rglob('*.md'))


def test_exit_save_default_and_export_does_not_clear_dirty(workspace):
    ui = UI(['hello','/export','/exit','save'])
    session = asyncio.run(conversation(config(workspace), Client(), ui))
    assert any('Save / Discard' in prompt for prompt in ui.prompts)
    assert session.destination.exists() and not session.dirty
    assert (workspace/'.skua/sessions'/f'{session.session_id}.md').exists()


def test_save_failure_cancel_blocks_exit_and_keeps_lock(workspace, monkeypatch):
    session = state(workspace)
    storage = store(workspace)
    save(session, storage)
    session.begin('new')
    session.finish('unsaved')
    original = storage.save
    def fail(*args, **kwargs):
        raise StorageError('disk full')
    monkeypatch.setattr(storage, 'save', fail)
    ui = UI(['save','cancel'])
    assert asyncio.run(ui.confirm_transition(session, storage)) is False
    assert session.dirty and session.lock is not None
    with pytest.raises(StorageError, match='locked'):
        storage.load(str(session.destination))
    monkeypatch.setattr(storage, 'save', original)
    session.close()


def test_save_retry_and_elsewhere(workspace, monkeypatch):
    session = state(workspace)
    storage = store(workspace)
    original = storage.save
    destinations = []
    def fail_twice(s, path, expected, **kwargs):
        destinations.append(path)
        if len(destinations) < 3:
            raise StorageError('disk full')
        return original(s, path, expected, **kwargs)
    monkeypatch.setattr(storage, 'save', fail_twice)
    ui = UI(['retry','save elsewhere','elsewhere.jsonl'])
    assert asyncio.run(ui.save(session, storage)) is True
    assert destinations[0] == destinations[1]
    assert session.destination == workspace/'elsewhere.jsonl'
    assert not session.dirty
    session.close()


def test_cancelled_switch_retains_current_history_and_lock(workspace):
    current = state(workspace)
    storage = store(workspace)
    save(current, storage)
    current.begin('unsaved')
    current.finish('continuation')
    candidate = state(workspace)
    path = save(candidate, storage)
    candidate.close()
    def inspect_locks():
        with pytest.raises(StorageError, match='locked'):
            storage.load(str(current.destination))
        other = storage.load(str(path))  # Cancel released the candidate lock.
        other.close()
        return '/exit'
    ui = UI([f'/resume {path}', 'cancel', inspect_locks, 'discard'])
    result = asyncio.run(conversation(config(workspace), Client(), ui, initial=current))
    assert result is current and result.dirty
    assert len(result.events) == 6


def test_failed_switch_keeps_current_session(workspace):
    current = state(workspace)
    ui = UI(['/resume missing.jsonl','/exit','discard'])
    result = asyncio.run(conversation(config(workspace), Client(), ui, initial=current))
    assert result is current
    assert any('does not exist' in text for text in ui.output)


def test_model_change_decline_before_client_construction(workspace):
    session = state(workspace)
    path = save(session, store(workspace))
    before = path.read_bytes()
    session.close()
    def factory(*args):
        pytest.fail('model client constructed before confirmation')
    asyncio.run(start(config(workspace,'other-model'), terminal=UI(['no']), client_factory=factory, resume=str(path)))
    assert path.read_bytes() == before
    resumed = store(workspace).load(str(path))
    resumed.close()


def test_wrong_root_before_client_construction(workspace):
    other = workspace/'other'
    other.mkdir()
    session = state(workspace)
    path = save(session, store(workspace))
    session.close()
    def factory(*args):
        pytest.fail('model client constructed for wrong root')
    with pytest.raises(StorageError, match='different project'):
        asyncio.run(start(config(other), terminal=UI([]), client_factory=factory, resume=str(path)))
    assert not (other/'.skua').exists()


def test_model_change_confirmed_recorded_only_in_memory(workspace):
    session = state(workspace)
    path = save(session, store(workspace))
    before = path.read_bytes()
    identity = ModelIdentity('default','https://different.example/v1','new-model')
    assert asyncio.run(UI(['yes']).confirm_model(session, identity))
    assert session.current_model == identity and session.dirty
    assert session.events[-1]['type'] == 'model_changed'
    assert path.read_bytes() == before
    assert not any('different.example' in str(m) for m in session.messages())
    session.close()


def test_new_cancel_and_save(workspace):
    ui = UI(['first','/new','cancel','/new','save','second','/exit','discard'])
    session = asyncio.run(conversation(config(workspace), Client(), ui))
    assert session.events[0]['text'] == 'second'
    paths = list((workspace/'.skua/sessions').glob('*.jsonl'))
    assert len(paths) == 1
    previous = store(workspace).load(str(paths[0]))
    assert previous.events[0]['text'] == 'first'
    previous.close()


def test_eof_does_not_save_automatically(workspace):
    def eof():
        raise EOFError()
    ui = UI(['hello', eof])
    session = asyncio.run(conversation(config(workspace), Client(), ui))
    assert session.dirty and not list(workspace.iterdir())


def test_export_refresh_after_resume_needs_no_replacement_prompt(workspace):
    session = state(workspace)
    storage = store(workspace)
    path = save(session, storage)
    markdown = workspace/'out.md'
    storage.export(session, markdown, None)
    session.close()
    resumed = storage.load(str(path))
    ui = UI([])
    assert asyncio.run(ui.save(resumed, storage, str(markdown), export=True))
    assert not ui.prompts and not resumed.dirty
    resumed.close()


def test_save_waits_until_turn_finishes(workspace):
    ready = asyncio.Event()
    release = asyncio.Event()
    class SlowClient(Client):
        async def complete(self, messages, **kwargs):
            ready.set()
            await release.wait()
            kwargs['on_text']('done')
            return Reply('done')
    ui = UI(['hello','/save','/exit'])
    async def scenario():
        task = asyncio.create_task(conversation(config(workspace), SlowClient(), ui))
        await ready.wait()
        # The queued save is not consumed until the active turn reaches stable state.
        assert len(ui.prompts) == 1
        assert not list(workspace.iterdir())
        release.set()
        return await task
    session = asyncio.run(scenario())
    assert not session.dirty and session.destination.exists()


def test_interrupted_transition_keeps_session_and_lock(workspace):
    session = state(workspace)
    save(session, store(workspace))
    session.begin('unsaved')
    session.finish('reply')
    def cancel():
        raise asyncio.CancelledError()
    def inspect():
        assert session.lock is not None and session.dirty
        return '/exit'
    ui = UI(['/new', cancel, inspect, 'discard'])
    result = asyncio.run(conversation(config(workspace), Client(), ui, initial=session))
    assert result is session


def test_accepted_cli_resume_uses_history_and_updates_original_save(workspace):
    from skua.client import ToolCall
    session = state(workspace)
    path = save(session, store(workspace))
    identity = session.session_id
    session.close()
    requests = []
    class Connected(Client):
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def complete(self, messages, **kwargs):
            requests.append(messages)
            if kwargs.get('tools', [{}])[0].get('function', {}).get('name') == 'skua_diagnostic':
                return Reply('', (ToolCall('skua_diagnostic', {'echo':'skua'}),))
            return await super().complete(messages, **kwargs)
    ui = UI(['yes','continue','/exit','save'])
    asyncio.run(start(config(workspace,'new-model'), terminal=ui,
                      client_factory=lambda *args: Connected(), resume=str(path)))
    assert len(requests) == 2
    assert len(requests[0]) == 1  # Capability probe never carries saved history.
    assert [message['content'] for message in requests[1][1:]] == ['hello','answer','continue']
    restored = store(workspace).load(str(path))
    assert restored.session_id == identity and not restored.dirty
    assert restored.current_model.model == 'new-model'
    assert restored.events[3]['type'] == 'model_changed'
    restored.close()
