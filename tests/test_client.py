import asyncio
from dataclasses import replace
import json

import httpx
import pytest

from skua.client import ChatClient, ModelError, check_capabilities
from skua.config import Auth, Profile

PROFILE = Profile('https://endpoint.example/v1', 'test-model', True, Auth(False))


class Stream(httpx.AsyncByteStream):
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.error:
            raise self.error

    async def aclose(self):
        self.closed = True


def event(delta=None, finish=None):
    return {'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]}


def sse(*events, done=True):
    return b''.join(b'data: ' + json.dumps(e, ensure_ascii=False).encode() + b'\r\n\r\n' for e in events) + (b'data: [DONE]\r\n\r\n' if done else b'')


def tool(index, ident=None, name=None, args=None):
    result = {'index': index, 'function': {}}
    if ident:
        result.update(id=ident, type='function')
    if name is not None:
        result['function']['name'] = name
    if args is not None:
        result['function']['arguments'] = args
    return result


def tool_schema(name):
    return {'type': 'function', 'function': {'name': name, 'parameters': {'type': 'object'}}}


def test_fragmented_utf8_sse_and_request():
    requests = []
    data = sse(event({'role': 'assistant'}), event({'content': 'héllo'}), event(finish='stop'))
    stream = Stream([data[i:i+1] for i in range(len(data))])
    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
    async def scenario():
        async with ChatClient(PROFILE, 'secret', transport=httpx.MockTransport(handler)) as client:
            deltas = []
            reply = await client.complete([{'role': 'user', 'content': 'hi'}], on_text=deltas.append)
            assert reply.text == 'héllo' and deltas == ['héllo']
        assert client.http.is_closed
    asyncio.run(scenario())
    assert stream.closed
    assert str(requests[0].url) == 'https://endpoint.example/v1/chat/completions'
    assert requests[0].headers['authorization'] == 'Bearer secret'
    assert 'tools' not in json.loads(requests[0].content)


def test_interleaved_tools_index_order_and_no_provider_ids():
    stream = Stream([sse(
        event({'tool_calls': [tool(1, 'remote_b', 'second', '{"b":'), tool(0, 'remote_a', 'first', '{"a":')]}),
        event({'tool_calls': [tool(0, args='1}'), tool(1, args='2}')]}), event(finish='tool_calls'),
    )])
    async def scenario():
        async with ChatClient(PROFILE, None, transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream))) as client:
            reply = await client.complete([], tools=[tool_schema('first'), tool_schema('second')])
            assert [c.name for c in reply.calls] == ['first', 'second']
            assert [c.arguments for c in reply.calls] == [{'a': 1}, {'b': 2}]
            assert 'remote_' not in repr(reply)
    asyncio.run(scenario())


def response_message(content='answer', calls=None, finish='stop'):
    return {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content, 'tool_calls': calls}, 'finish_reason': finish}]}


def test_nonstream_and_tool_diagnostic():
    calls = [{'id': 'remote', 'type': 'function', 'function': {'name': 'skua_diagnostic', 'arguments': '{"echo":"skua"}'}}]
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_message(None, calls, 'tool_calls'))
    async def scenario():
        async with ChatClient(replace(PROFILE, stream=False), None, transport=httpx.MockTransport(handler)) as client:
            await check_capabilities(client)
    asyncio.run(scenario())
    assert requests[0]['stream'] is False
    assert requests[0]['tool_choice']['function']['name'] == 'skua_diagnostic'


@pytest.mark.parametrize('body', [
    response_message(None, [{'id': 'a', 'type': 'function', 'function': {'name': 'known', 'arguments': '{bad'}}], 'tool_calls'),
    response_message(None, [{'id': 'a', 'type': 'function', 'function': {'name': 'known', 'arguments': '[]'}}], 'tool_calls'),
    response_message(None, [{'id': 'a', 'type': 'function', 'function': {'name': 'known', 'arguments': '{"x":1,"x":2}'}}], 'tool_calls'),
    response_message(None, [{'id': 'a', 'type': 'function', 'function': {'name': 'unknown', 'arguments': '{}'}}], 'tool_calls'),
    response_message('partial', finish='length'),
    {'choices': []}, {'error': {'message': 'SECRET'}},
])
def test_invalid_responses_are_sanitized_not_retried(body):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=body)
    async def scenario():
        async with ChatClient(replace(PROFILE, stream=False), None, transport=httpx.MockTransport(handler), delays=(0,0,0)) as client:
            with pytest.raises(ModelError) as error:
                await client.complete([], tools=[tool_schema('known')])
            assert 'SECRET' not in str(error.value)
    asyncio.run(scenario())
    assert len(requests) == 1


@pytest.mark.parametrize('status,count', [(401,1), (400,1), (404,1), (302,1), (429,4), (503,4)])
def test_status_retry_policy(status, count):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, text='SECRET', headers={'Location': 'https://other.example'})
    async def scenario():
        async with ChatClient(PROFILE, 'key', transport=httpx.MockTransport(handler), delays=(0,0,0)) as client:
            with pytest.raises(ModelError) as error:
                await client.complete([])
            assert 'SECRET' not in str(error.value) and str(status) in str(error.value)
    asyncio.run(scenario())
    assert len(requests) == count


@pytest.mark.parametrize('partial', [False, True])
def test_connection_failure_retry_only_before_payload(partial):
    streams = []
    def handler(_):
        data = sse(event({'content': 'partial'}), done=False) if partial else b''
        stream = Stream([data], httpx.ReadError('SECRET'))
        streams.append(stream)
        return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
    async def scenario():
        deltas = []
        async with ChatClient(PROFILE, None, transport=httpx.MockTransport(handler), delays=(0,0,0)) as client:
            with pytest.raises(ModelError):
                await client.complete([], on_text=deltas.append)
        assert deltas == (['partial'] if partial else [])
    asyncio.run(scenario())
    assert len(streams) == (1 if partial else 4)
    assert all(s.closed for s in streams)


@pytest.mark.parametrize('data', [sse(event({'content':'partial'}), done=False), sse(event({'content':'partial'})), b'data: {broken}\n\n', b'data: {'])
def test_malformed_or_incomplete_stream(data):
    async def scenario():
        stream = Stream([data])
        async with ChatClient(PROFILE, None, transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream))) as client:
            with pytest.raises(ModelError):
                await client.complete([])
        assert stream.closed
    asyncio.run(scenario())


def test_cancel_closes_stream_without_retry():
    async def scenario():
        ready = asyncio.Event()
        class Blocking(Stream):
            async def __aiter__(self):
                yield sse(event({'content': 'partial'}), done=False)
                ready.set()
                await asyncio.Event().wait()
        stream = Blocking([])
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        async with ChatClient(PROFILE, None, transport=httpx.MockTransport(handler)) as client:
            task = asyncio.create_task(client.complete([]))
            await ready.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert stream.closed and len(requests) == 1
    asyncio.run(scenario())


def test_cancel_retry_wait():
    async def scenario():
        waiting = asyncio.Event()
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503)
        async with ChatClient(PROFILE, None, transport=httpx.MockTransport(handler), delays=(60,60,60)) as client:
            task = asyncio.create_task(client.complete([], on_retry=lambda *_: waiting.set()))
            await waiting.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert len(calls) == 1
    asyncio.run(scenario())


def test_missing_tool_support_fails():
    async def scenario():
        async with ChatClient(replace(PROFILE, stream=False), None, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response_message()))) as client:
            with pytest.raises(ModelError, match='tool-calling'):
                await check_capabilities(client)
    asyncio.run(scenario())


def test_retry_recovers_without_duplicate_text():
    async def scenario():
        attempts = []
        retries = []
        def handler(request):
            attempts.append(request)
            if len(attempts) < 3:
                return httpx.Response(503)
            return httpx.Response(200, json=response_message('done'))
        async with ChatClient(replace(PROFILE, stream=False), None, transport=httpx.MockTransport(handler), delays=(0,0,0)) as client:
            text = []
            reply = await client.complete([], on_text=text.append, on_retry=lambda n,d: retries.append(n))
            assert reply.text == 'done' and text == ['done']
        assert retries == [1,2]
    asyncio.run(scenario())


def test_tool_argument_payload_prevents_transport_retry():
    attempts = []
    def handler(request):
        attempts.append(request)
        stream = Stream([sse(event({'tool_calls':[tool(0,'a','known','{')]}), done=False)], httpx.ReadError('dropped'))
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=stream)
    async def scenario():
        async with ChatClient(PROFILE, None, transport=httpx.MockTransport(handler), delays=(0,0,0)) as client:
            with pytest.raises(ModelError):
                await client.complete([], tools=[tool_schema('known')])
    asyncio.run(scenario())
    assert len(attempts) == 1


@pytest.mark.parametrize('arguments', ['{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'])
def test_nonfinite_tool_arguments_rejected(arguments):
    body = response_message(None, [{'id':'a','type':'function','function':{'name':'known','arguments':arguments}}], 'tool_calls')
    async def scenario():
        async with ChatClient(replace(PROFILE,stream=False), None, transport=httpx.MockTransport(lambda _: httpx.Response(200,json=body))) as client:
            with pytest.raises(ModelError):
                await client.complete([], tools=[tool_schema('known')])
    asyncio.run(scenario())


def test_response_limits(monkeypatch):
    monkeypatch.setattr('skua.client.MAX_FRAME_BYTES', 32)
    async def scenario():
        stream = Stream([b'data: ' + b'x'*33])
        async with ChatClient(PROFILE, None, transport=httpx.MockTransport(lambda _: httpx.Response(200,headers={'content-type':'text/event-stream'},stream=stream))) as client:
            with pytest.raises(ModelError, match='limit'):
                await client.complete([])
        assert stream.closed
    asyncio.run(scenario())


def test_total_deadline_and_cleanup():
    async def scenario():
        class Block(Stream):
            async def __aiter__(self):
                await asyncio.Event().wait()
                yield b''
        streams = []
        def handler(request):
            stream = Block([])
            streams.append(stream)
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=stream)
        async with ChatClient(PROFILE, None, transport=httpx.MockTransport(handler), delays=(), deadline=0.01) as client:
            with pytest.raises(ModelError, match='timed out'):
                await client.complete([])
        assert len(streams)==1 and streams[0].closed
    asyncio.run(scenario())


def test_sse_comments_multiline_json_and_usage():
    data = (b': keepalive\r\n\r\ndata: {"choices":\r\ndata: [{"index":0,"delta":{"content":"ok"},"finish_reason":"stop"}]}\r\n\r\n'
            b'data: {"choices":[],"usage":{"total_tokens":3}}\n\ndata: [DONE]\n\n')
    async def scenario():
        async with ChatClient(PROFILE, None, transport=httpx.MockTransport(lambda _: httpx.Response(200,headers={'content-type':'text/event-stream'},stream=Stream([data])))) as client:
            assert (await client.complete([])).text == 'ok'
    asyncio.run(scenario())


@pytest.mark.parametrize('field', ['prompt_filter_results', 'prompt_annotations'])
@pytest.mark.parametrize('with_tool', [False, True])
def test_azure_prompt_metadata_before_completion(field, with_tool):
    metadata = {'choices': [], 'created': 0, 'id': '', 'model': '', 'object': '',
                field: [{'prompt_index': 0, 'content_filter_results': {'hate': {'filtered': False, 'severity': 'safe'}}}]}
    completion = ([event({'tool_calls': [tool(0, 'remote', 'skua_diagnostic', '{"echo":"skua"}')]}), event(finish='tool_calls')]
                  if with_tool else [event({'content': 'hello'}), event(finish='stop')])
    stream = Stream([sse(metadata, *completion)])
    async def scenario():
        async with ChatClient(PROFILE, None, transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream))) as client:
            if with_tool:
                await check_capabilities(client)
            else:
                assert (await client.complete([])).text == 'hello'
        assert stream.closed
    asyncio.run(scenario())


def test_prompt_metadata_alone_is_not_a_completion():
    stream = Stream([sse({'choices': [], 'prompt_filter_results': []})])
    async def scenario():
        async with ChatClient(PROFILE, None, transport=httpx.MockTransport(lambda _: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream))) as client:
            with pytest.raises(ModelError, match='before completion'):
                await client.complete([])
    asyncio.run(scenario())
