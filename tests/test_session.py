import asyncio

from skua.agent import run_turn
from skua.client import ModelError, Reply
from skua.session import ModelIdentity, Session


def session(root):
    return Session(root, ModelIdentity('local', 'https://example/v1', 'model'))


def test_complete_turn_context_and_no_writes(workspace):
    state = session(workspace)
    class Client:
        async def complete(self, messages, **kwargs):
            assert messages[-1] == {'role': 'user', 'content': 'hello'}
            assert 'tools' not in kwargs
            kwargs['on_text']('hi')
            return Reply('hi')
    asyncio.run(run_turn(state, Client(), 'hello'))
    assert [e['type'] for e in state.events] == ['message', 'message', 'turn_end']
    assert [e['seq'] for e in state.events] == [1,2,3]
    assert all(e['turn'] == 1 for e in state.events)
    assert state.active_turn is None
    assert list(workspace.iterdir()) == []


def test_interrupted_turn_is_retained_but_not_executable(workspace):
    state = session(workspace)
    class Client:
        async def complete(self, messages, **kwargs):
            kwargs['on_text']('part')
            raise ModelError('invalid_response', 'Invalid response.')
    asyncio.run(run_turn(state, Client(), 'hello'))
    assert state.events[-1]['status'] == 'error'
    assert state.events[-2]['completion'] == 'interrupted'
    assert 'incomplete' in state.messages()[-1]['content']
    assert all(e['type'] not in {'tool_call','tool_result'} for e in state.events)
    assert all(m['content'] != 'Invalid response.' for m in state.messages())


def test_cancel_ends_turn_and_keeps_partial_text(workspace):
    state = session(workspace)
    async def scenario():
        ready = asyncio.Event()
        class Client:
            async def complete(self, messages, **kwargs):
                kwargs['on_text']('partial')
                ready.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(run_turn(state, Client(), 'hello'))
        await ready.wait()
        task.cancel()
        await task
    asyncio.run(scenario())
    assert state.active_turn is None
    assert state.events[-1]['status'] == 'cancelled'
    assert state.events[-2]['text'] == 'partial'
    assert list(workspace.iterdir()) == []
