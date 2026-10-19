import asyncio
import json

import httpx
import pytest

from skua.client import ChatClient, ModelError
from skua.config import build_config
from skua.squak import full_checks
from test_client import Stream, event, sse, tool


def test_full_diagnostic_uses_only_synthetic_context(workspace):
    cfg = build_config(workspace, [('test', {'schema_version':1, 'profiles': {'default': {'base_url':'https://example/v1','model':'model','auth':{'enabled':False}}}})])
    requests = []
    streams = []
    def handler(request):
        requests.append(request)
        stream = Stream([sse(event({'tool_calls':[tool(0,'remote','skua_diagnostic','{"echo":"skua"}')]}), event(finish='tool_calls'))])
        streams.append(stream)
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=stream)
    def factory(profile, credential):
        assert credential is None
        return ChatClient(profile, credential, transport=httpx.MockTransport(handler))
    output = []
    assert asyncio.run(full_checks(cfg, output.append, client_factory=factory)) == 0
    assert len(requests) == 1 and all(s.closed for s in streams)
    payload = json.loads(requests[0].content)
    assert str(workspace) not in str(payload)
    assert len(payload['messages']) == 1
    assert any('may consume tokens' in line for line in output)
    assert any('checks passed (streaming)' in line for line in output)
    assert list(workspace.iterdir()) == []


def test_diagnostic_failure_is_not_success(workspace):
    cfg = build_config(workspace, [('test', {'schema_version':1, 'profiles': {'default': {'base_url':'https://example/v1','model':'model','auth':{'enabled':False}}}})])
    def factory(profile, credential):
        return ChatClient(profile, credential, transport=httpx.MockTransport(lambda _: httpx.Response(401, text='secret')))
    output = []
    with pytest.raises(ModelError, match='401'):
        asyncio.run(full_checks(cfg, output.append, client_factory=factory))
    assert not any('Endpoint and native' in line for line in output)
