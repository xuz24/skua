"""Chat Completions adapter; provider IDs stay inside this module."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
from typing import Callable

import httpx

from .config import Profile

MAX_RESPONSE_BYTES = 8_000_000
MAX_FRAME_BYTES = 1_000_000


class ModelError(Exception):
    """Sanitized errors: never include endpoint bodies, credentials, or arguments."""
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


@dataclass(frozen=True)
class Reply:
    text: str
    calls: tuple[ToolCall, ...] = ()


def _invalid() -> ModelError:
    return ModelError('invalid_response', 'Endpoint returned an invalid or unsupported response.')


def _json(value: str | bytes):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = item
        return result
    def constant(_):
        raise ValueError('non-finite number')
    def floating(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError('non-finite number')
        return result
    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=constant, parse_float=floating)
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid() from None


class Assembly:
    def __init__(self, allowed: set[str], on_text: Callable[[str], None]):
        self.allowed = allowed
        self.on_text = on_text
        self.text: list[str] = []
        self.calls: dict[int, dict[str, str]] = {}
        self.finish: str | None = None
        self.started = False

    def chunk(self, value: object, *, streaming: bool):
        if not isinstance(value, dict) or 'error' in value:
            raise _invalid()
        choices = value.get('choices')
        # Azure-backed gateways can send prompt-filter annotations before the
        # first completion delta. These are metadata, like a usage-only chunk;
        # they must not mark the response complete or start tool dispatch.
        metadata = (isinstance(value.get('usage'), dict) or any(
            isinstance(value.get(field), list)
            and all(isinstance(item, dict) for item in value[field])
            for field in ('prompt_filter_results', 'prompt_annotations')
        ))
        if choices == [] and streaming and metadata:
            return
        if not isinstance(choices, list) or len(choices) != 1:
            raise _invalid()
        choice = choices[0]
        if not isinstance(choice, dict) or type(choice.get('index')) is not int or choice['index'] != 0:
            raise _invalid()
        if self.finish is not None:
            raise _invalid()
        delta = choice.get('delta' if streaming else 'message')
        if not isinstance(delta, dict) or delta.get('role', 'assistant') != 'assistant':
            raise _invalid()
        if delta.get('function_call') is not None:
            raise ModelError('unsupported_tools', 'Endpoint uses unsupported legacy function calls; native tool calling is required.')
        content = delta.get('content')
        refusal = delta.get('refusal')
        if content is not None and not isinstance(content, str):
            raise _invalid()
        if refusal is not None and not isinstance(refusal, str):
            raise _invalid()
        for text in (content, refusal):
            if text:
                self.started = True
                self.text.append(text)
                self.on_text(text)
        calls = delta.get('tool_calls')
        if calls is not None:
            if not isinstance(calls, list):
                raise _invalid()
            for position, call in enumerate(calls):
                self.started = True
                if not isinstance(call, dict):
                    raise _invalid()
                index = call.get('index') if streaming else position
                if type(index) is not int or not 0 <= index < 128:
                    raise _invalid()
                current = self.calls.setdefault(index, {'id': '', 'name': '', 'arguments': '', 'type': ''})
                for field in ('id', 'type'):
                    if field in call:
                        item = call[field]
                        if not isinstance(item, str) or not item or (current[field] and current[field] != item):
                            raise _invalid()
                        current[field] = item
                function = call.get('function', {})
                if not isinstance(function, dict):
                    raise _invalid()
                for field in ('name', 'arguments'):
                    if field in function:
                        if not isinstance(function[field], str):
                            raise _invalid()
                        current[field] += function[field]
        finish = choice.get('finish_reason')
        if finish is not None:
            if finish not in ('stop', 'tool_calls', 'length', 'content_filter'):
                raise _invalid()
            self.finish = finish

    def result(self) -> Reply:
        if self.finish is None:
            raise ModelError('interrupted_response', 'Endpoint response ended before completion.')
        if self.finish in ('length', 'content_filter'):
            raise ModelError('incomplete_response', 'Endpoint stopped before a complete response was available.')
        if bool(self.calls) != (self.finish == 'tool_calls'):
            raise _invalid()
        if sorted(self.calls) != list(range(len(self.calls))):
            raise _invalid()
        calls = []
        ids = set()
        for index in sorted(self.calls):
            call = self.calls[index]
            if not call['id'] or call['id'] in ids or call['type'] != 'function':
                raise _invalid()
            ids.add(call['id'])
            if call['name'] not in self.allowed:
                raise ModelError('unsupported_tools', 'Endpoint requested an unavailable tool. Nothing was executed.')
            args = _json(call['arguments'])
            if not isinstance(args, dict):
                raise _invalid()
            calls.append(ToolCall(call['name'], args))
        return Reply(''.join(self.text), tuple(calls))


async def _events(response: httpx.Response):
    pending = b''
    data: list[bytes] = []
    total = frame_size = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ModelError('response_limit', 'Endpoint response exceeded the 8 MB limit.')
        pending += chunk
        while b'\n' in pending:
            line, pending = pending.split(b'\n', 1)
            line = line.removesuffix(b'\r')
            frame_size += len(line)
            if frame_size > MAX_FRAME_BYTES:
                raise ModelError('response_limit', 'Endpoint stream frame exceeded the 1 MB limit.')
            if not line:
                if data:
                    yield b'\n'.join(data)
                data = []
                frame_size = 0
            elif line.startswith(b'data:'):
                data.append(line[5:].removeprefix(b' '))
        if len(pending) + frame_size > MAX_FRAME_BYTES:
            raise ModelError('response_limit', 'Endpoint stream frame exceeded the 1 MB limit.')
    if pending or data:
        raise ModelError('interrupted_response', 'Endpoint stream ended in an incomplete event.')


class ChatClient:
    def __init__(self, profile: Profile, credential: str | None, *, transport=None,
                 delays: tuple[float, ...] = (1, 2, 4), deadline: float = 300):
        self.profile = profile
        self.delays = delays
        self.deadline = deadline
        headers = {'Authorization': f'Bearer {credential}'} if credential else {}
        try:
            self.http = httpx.AsyncClient(headers=headers, transport=transport,
                                         timeout=httpx.Timeout(60, connect=10), follow_redirects=False)
        except (ValueError, UnicodeError):
            raise ModelError('invalid_credentials', 'Credential cannot be encoded as an HTTP header.') from None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.http.aclose()

    async def complete(self, messages: list[dict], *, tools: list[dict] | None = None,
                       tool_choice=None, on_text: Callable[[str], None] = lambda _: None,
                       on_retry: Callable[[int, float], None] = lambda *_: None) -> Reply:
        payload = {'model': self.profile.model, 'messages': messages, 'stream': self.profile.stream}
        if tools:
            payload['tools'] = tools
        if tool_choice is not None:
            payload['tool_choice'] = tool_choice
        allowed = {tool['function']['name'] for tool in tools or []}
        for attempt in range(len(self.delays) + 1):
            assembly = Assembly(allowed, on_text)
            retry = False
            try:
                async with asyncio.timeout(self.deadline):
                    async with self.http.stream('POST', self.profile.base_url.rstrip('/') + '/chat/completions', json=payload) as response:
                        if response.status_code != 200:
                            retry = response.status_code in {408, 429, 500, 502, 503, 504}
                            raise ModelError('temporary_endpoint' if retry else 'endpoint_rejected',
                                             f'Endpoint rejected the request (HTTP {response.status_code}); check endpoint, model, authentication, and capabilities.')
                        if self.profile.stream:
                            if response.headers.get('content-type', '').split(';')[0].strip() != 'text/event-stream':
                                raise ModelError('unsupported_stream', 'Endpoint did not return an event stream; check its streaming support.')
                            done = False
                            async for event in _events(response):
                                if event == b'[DONE]':
                                    done = True
                                    break
                                assembly.chunk(_json(event), streaming=True)
                            if not done:
                                raise ModelError('interrupted_response', 'Endpoint stream ended without its completion marker.')
                        else:
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > MAX_RESPONSE_BYTES:
                                    raise ModelError('response_limit', 'Endpoint response exceeded the 8 MB limit.')
                            assembly.chunk(_json(bytes(body)), streaming=False)
                        return assembly.result()
            except (httpx.InvalidURL, httpx.LocalProtocolError):
                error = ModelError('invalid_request', 'Invalid endpoint URL or request headers; check configuration.')
            except httpx.DecodingError:
                error = ModelError('invalid_response', 'Endpoint returned invalid content encoding.')
            except (httpx.TransportError, TimeoutError):
                retry = not assembly.started
                error = ModelError('transport_error', 'Endpoint connection failed or timed out.' + (' Partial response retained; not retried.' if assembly.started else ''))
            except ModelError as exc:
                error = exc
            if not retry or attempt == len(self.delays):
                raise error from None
            on_retry(attempt + 1, self.delays[attempt])
            await asyncio.sleep(self.delays[attempt])
        raise AssertionError('unreachable')


DIAGNOSTIC_TOOL = {
    'type': 'function', 'function': {
        'name': 'skua_diagnostic', 'description': 'Return the diagnostic echo; no action is executed.',
        'parameters': {'type': 'object', 'properties': {'echo': {'type': 'string', 'enum': ['skua']}},
                       'required': ['echo'], 'additionalProperties': False},
    },
}


async def check_capabilities(client: ChatClient, on_retry=lambda *_: None) -> None:
    reply = await client.complete(
        [{'role': 'user', 'content': 'Call skua_diagnostic once with echo set to skua.'}],
        tools=[DIAGNOSTIC_TOOL], tool_choice={'type': 'function', 'function': {'name': 'skua_diagnostic'}},
        on_retry=on_retry,
    )
    if len(reply.calls) != 1 or reply.calls[0].arguments != {'echo': 'skua'}:
        raise ModelError('unsupported_tools', 'Native tool-calling diagnostic failed. No diagnostic calls were executed.')
