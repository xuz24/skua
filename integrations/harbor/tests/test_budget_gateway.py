from concurrent.futures import ThreadPoolExecutor
import json

from skua_harbor.budget_gateway import Ledger, event_stream


def test_atomic_reservation_enforces_global_and_route_limits(tmp_path):
    ledger = Ledger(tmp_path / 'budget.json', total_limit=.025, route_limits={'a': .02, 'b': .02})
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: ledger.reserve('a', {'messages': []}, 8192), range(20)))
    assert sum(v is not None for v in values) == 1
    assert ledger.reserve('b', {'messages': []}, 8192) is not None
    assert ledger.reserve('b', {'messages': []}, 8192) is None
    assert json.loads(ledger.path.read_text())['accounted_usd'] <= .025


def test_unknown_usage_keeps_reservation_and_valid_usage_releases_it(tmp_path):
    ledger = Ledger(tmp_path / 'budget.json', total_limit=1, route_limits={'a': 1})
    first = ledger.reserve('a', {'messages': []}, 1000)
    ledger.finish(first, 'error')
    assert ledger.records[0]['charged_usd'] == ledger.records[0]['reserved_usd']
    second = ledger.reserve('a', {'messages': []}, 1000)
    ledger.finish(second, 'completed', {'prompt_tokens': 20, 'completion_tokens': 10})
    assert ledger.records[1]['charged_usd'] < ledger.records[1]['reserved_usd']


def test_reported_usage_above_bound_halts_all_routes(tmp_path):
    ledger = Ledger(tmp_path / 'budget.json', total_limit=1, route_limits={'a': 1})
    index = ledger.reserve('a', {'messages': []}, 100)
    ledger.finish(index, 'completed', {'prompt_tokens': 100, 'completion_tokens': 101})
    assert ledger.halted and ledger.reserve('a', {'messages': []}, 100) is None


def test_stream_conversion_keeps_function_calls_and_finish_reason():
    result = event_stream({'choices': [{'message': {'content': None, 'tool_calls': [
        {'id': 'tool', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
        'finish_reason': 'tool_calls'}]})
    assert b'"index": 0' in result and b'data: [DONE]' in result


def test_gateway_forwards_only_selected_model_and_accounts_usage(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    import urllib.request
    import urllib.error
    import pytest
    from skua_harbor.budget_gateway import Gateway

    received = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            received.append((self.headers['Authorization'], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            data = json.dumps({'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}],
                               'usage': {'prompt_tokens': 10, 'completion_tokens': 2}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    ledger = Ledger(tmp_path / 'budget.json', total_limit=.02, route_limits={'/v1/test': .02})
    gateway = Gateway(ledger, endpoint=f'http://127.0.0.1:{upstream.server_port}',
                      model='gpt-5.6-luna', credential='private-key')
    for server in (upstream, gateway):
        Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f'http://127.0.0.1:{gateway.server_port}/v1/test/chat/completions'
        def request(model, token):
            return urllib.request.Request(url, data=json.dumps({'model': model, 'messages': [], 'stream': True,
                'max_completion_tokens': 999999}).encode(), headers={'Authorization': 'Bearer ' + token})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(request('gpt-5.6-luna', 'wrong'))
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(request('expensive-model', gateway.token))
        image_request = urllib.request.Request(url, data=json.dumps({'model': 'gpt-5.6-luna',
            'messages': [{'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': 'private'}}]}]}).encode(),
            headers={'Authorization': 'Bearer ' + gateway.token})
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(image_request)
        assert not received
        assert not ledger.records
        with urllib.request.urlopen(request('gpt-5.6-luna', gateway.token)) as response:
            assert b'[DONE]' in response.read()
        assert received[0][0] == 'Bearer private-key'
        assert received[0][1]['max_completion_tokens'] == 8192
        assert received[0][1]['stream'] is False
        assert ledger.records[0]['usage']['prompt_tokens'] == 10
        assert ledger.records[0]['finish_reason'] == 'stop'
        assert 'private-key' not in ledger.path.read_text()
    finally:
        for server in (gateway, upstream):
            server.shutdown()
            server.server_close()


def test_finish_reason_is_allowlisted_and_does_not_change_cost(tmp_path):
    ledger = Ledger(tmp_path / 'budget.json', total_limit=1, route_limits={'a': 1})
    for reason in ('length', 'content_filter', 'private-provider-text'):
        index = ledger.reserve('a', {'messages': []}, 100)
        ledger.finish(index, 'completed', {'prompt_tokens': 10, 'completion_tokens': 100}, reason)
        assert ledger.records[-1]['finish_reason'] == (reason if reason != 'private-provider-text' else None)
    assert 'private-provider-text' not in ledger.path.read_text()
