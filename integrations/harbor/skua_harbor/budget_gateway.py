"""Evaluation-only gateway: reserve cost before dispatch; credentials stay on host.

Reservations use deliberately conservative byte-based input bounds and an explicit
completion-token cap. Confirmed usage is priced at the selected published rates
(no cache discount). Missing/error usage keeps the entire reservation charged.
This is an estimated spending control, not an authoritative provider invoice.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
from threading import Lock
import time
import urllib.error
import urllib.request

INPUT_RATE = .20 / 1_000_000
OUTPUT_RATE = 1.20 / 1_000_000
MAX_BODY = 256_000


class Ledger:
    def __init__(self, path, *, total_limit, route_limits):
        self.path = Path(path)
        self.limit = total_limit
        self.route_limits = route_limits
        self.records = []
        self.denials = []
        self.lock = Lock()
        self.halted = False
        if self.path.exists():
            raise ValueError('A new ledger path is required; never reset an existing budget')
        self._write()

    def _write(self):
        data = {'version': 1, 'limit_usd': self.limit, 'halted': self.halted,
                'accounted_usd': sum(r['charged_usd'] for r in self.records),
                'denials': self.denials, 'requests': self.records}
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data, indent=2) + '\n')
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def reserve(self, route, body, max_completion_tokens):
        # Text tokenizers encode UTF-8 bytes plus message/tool framing. Two times
        # serialized bytes + ample framing reserve intentionally overcounts inputs.
        raw = json.dumps(body, ensure_ascii=False).encode()
        tokens = len(raw) * 2 + 128 * (len(body.get('messages', [])) + len(body.get('tools', []))) + 2048
        reserved = tokens * INPUT_RATE + max_completion_tokens * OUTPUT_RATE
        with self.lock:
            charged = sum(r['charged_usd'] for r in self.records)
            routed = sum(r['charged_usd'] for r in self.records if r['route'] == route)
            if self.halted or route not in self.route_limits or charged + reserved > self.limit or routed + reserved > self.route_limits[route]:
                self.denials.append({'route': route, 'at': time.time(), 'required_usd': reserved})
                self._write()
                return None
            record = {'id': len(self.records) + 1, 'route': route, 'started': time.time(),
                      'request_sha256': hashlib.sha256(raw).hexdigest(),
                      'input_bound_tokens': tokens, 'max_completion_tokens': max_completion_tokens,
                      'reserved_usd': reserved, 'charged_usd': reserved,
                      'status': 'reserved', 'usage': None, 'estimated_actual_usd': None}
            self.records.append(record)
            self._write()  # Durable reservation precedes the network request.
            return record['id']

    def finish(self, identifier, status, usage=None, finish_reason=None):
        with self.lock:
            record = self.records[identifier - 1]
            record['status'] = status
            record['finished'] = time.time()
            record['finish_reason'] = finish_reason if finish_reason in ('stop', 'tool_calls', 'length', 'content_filter') else None
            if isinstance(usage, dict):
                incoming, outgoing = usage.get('prompt_tokens'), usage.get('completion_tokens')
                if type(incoming) is int and type(outgoing) is int and incoming >= 0 and outgoing >= 0:
                    cost = incoming * INPUT_RATE + outgoing * OUTPUT_RATE
                    record['usage'] = {'prompt_tokens': incoming, 'completion_tokens': outgoing}
                    record['estimated_actual_usd'] = cost
                    if incoming > record['input_bound_tokens'] or outgoing > record['max_completion_tokens']:
                        self.halted = True  # Do not assume the configured bounds hold after a violation.
                        record['status'] = 'usage_bound_exceeded'
                        record['charged_usd'] = max(cost, record['reserved_usd'])
                    else:
                        record['charged_usd'] = cost
            self._write()


def event_stream(document):
    choice, = document['choices']
    message = choice['message']
    delta = {'role': 'assistant', 'content': message.get('content')}
    if message.get('tool_calls'):
        delta['tool_calls'] = [dict(call, index=i) for i, call in enumerate(message['tool_calls'])]
    if message.get('refusal'):
        delta['refusal'] = message['refusal']
    frames = [{'choices': [{'index': 0, 'delta': delta, 'finish_reason': None}]},
              {'choices': [{'index': 0, 'delta': {}, 'finish_reason': choice['finish_reason']}]}]
    return (''.join('data: ' + json.dumps(f) + '\n\n' for f in frames) + 'data: [DONE]\n\n').encode()


class Gateway(ThreadingHTTPServer):
    def __init__(self, ledger, *, endpoint, model, credential, max_completion_tokens=8192):
        if model != 'gpt-5.6-luna':
            raise ValueError('Published evaluation rates are specific to gpt-5.6-luna')
        self.ledger = ledger
        self.endpoint = endpoint.rstrip('/') + '/chat/completions'
        self.model = model
        self.credential = credential
        self.token = secrets.token_urlsafe(32)
        self.completion_limit = max_completion_tokens
        super().__init__(('0.0.0.0', 0), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, content, content_type='application/json'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(content)))
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def reject(self, status, code):
        self.send(status, json.dumps({'error': {'code': code}}).encode())

    def do_POST(self):
        gateway = self.server
        if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + gateway.token):
            return self.reject(401, 'gateway_authentication')
        route = self.path.removesuffix('/chat/completions')
        if self.path == route or route not in gateway.ledger.route_limits:
            return self.reject(404, 'unknown_route')
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                return self.reject(413, 'request_limit')
            payload = json.loads(self.rfile.read(length))
            if payload.get('model') != gateway.model or not isinstance(payload.get('messages'), list):
                return self.reject(400, 'invalid_model_or_messages')
            if any(not isinstance(m, dict) or
                   (m.get('content') is not None and not isinstance(m['content'], str))
                   for m in payload['messages']):
                return self.reject(400, 'text_only_messages_required')
            streaming = payload.get('stream', False)
            body = {key: payload[key] for key in ('messages', 'tools', 'tool_choice') if key in payload}
            body.update(model=gateway.model, stream=False, max_completion_tokens=gateway.completion_limit)
        except (ValueError, TypeError, AttributeError):
            return self.reject(400, 'invalid_request')
        identifier = gateway.ledger.reserve(route, body, gateway.completion_limit)
        if identifier is None:
            return self.reject(402, 'evaluation_budget_exhausted')
        try:
            request = urllib.request.Request(gateway.endpoint, data=json.dumps(body).encode(),
                headers={'Authorization': 'Bearer ' + gateway.credential, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=90) as response:
                data = response.read(8_000_001)
                if len(data) > 8_000_000:
                    raise ValueError('response limit')
            document = json.loads(data)
            output = event_stream(document) if streaming else data
            gateway.ledger.finish(identifier, 'completed', document.get('usage'),
                                  document['choices'][0].get('finish_reason'))
            self.send(200, output, 'text/event-stream' if streaming else 'application/json')
        except urllib.error.HTTPError as exc:
            gateway.ledger.finish(identifier, 'upstream_http_' + str(exc.code))
            # Do not retry a potentially charged request automatically.
            self.reject(400, 'upstream_rejected')
        except Exception:
            gateway.ledger.finish(identifier, 'upstream_error_or_timeout')
            self.reject(400, 'upstream_error_or_timeout')
