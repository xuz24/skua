"""Synthetic Chat Completions server; no real provider or credentials."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        prompt = next(m['content'] for m in data['messages'] if m['role'] == 'user')
        calls = []
        if not any(m['role'] == 'tool' for m in data['messages']):
            if 'mode: timeout' in prompt:
                name, args = 'run_command', {'command': 'printf started; sleep 30'}
            elif 'mode: certificate-key' in prompt:
                name, args = 'run_command', {'command':
                    "mkdir -p /workspace/ssl /etc/nginx/ssl && "
                    "printf fixture > /workspace/ssl/server.key && "
                    "printf fixture > /etc/nginx/ssl/localhost.key && "
                    "chmod 600 /workspace/ssl/server.key /etc/nginx/ssl/localhost.key && "
                    "printf 'harbor-ready\\n' > /workspace/answer.txt && "
                    "printf 'external-ready\\n' > /tmp/skua-fixture.txt"}
            elif 'mode: verified-tool-failure' in prompt:
                name, args = 'run_command', {'command':
                    "printf 'harbor-ready\\n' > /workspace/answer.txt && "
                    "printf 'external-ready\\n' > /tmp/skua-fixture.txt && exit 7"}
            elif 'mode: failure' in prompt:
                name, args = 'run_command', {'command': 'exit 7'}
            else:
                name, args = 'apply_patch', {'message': 'Synthetic fixture', 'changes': [
                    {'op': 'write', 'path': 'answer.txt', 'content': 'harbor-ready\n'}]}
            calls = [{'index': 0, 'id': 'fixture-call', 'type': 'function',
                      'function': {'name': name, 'arguments': json.dumps(args)}}]
            if 'mode: success' in prompt:
                operations = [
                    ('list_files', {'path': '/workspace'}),
                    ('read_file', {'path': '/etc/os-release'}),
                    ('run_command', {'command': "python3 - <<'PY'\nfrom pathlib import Path\nvals = [3, 6]\nPath('/tmp/skua-fixture.txt').write_text(str(sum(vals)/3))\nPY\nprintf ok >/dev/null"}),
                    ('apply_patch', {'message': 'External container task file', 'changes': [
                        {'op': 'write', 'path': '/tmp/skua-fixture.txt', 'content': 'external-ready\n'}]}),
                    (name, args),
                ]
                calls = [{'index': i, 'id': f'fixture-{i}', 'type': 'function',
                          'function': {'name': n, 'arguments': json.dumps(a)}} for i, (n, a) in enumerate(operations)]
        frames = [{'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': 'Fixture response',
                   **({'tool_calls': calls} if calls else {})}, 'finish_reason': None}]},
                  {'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'tool_calls' if calls else 'stop'}]}]
        body = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames) + 'data: [DONE]\n\n'
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(body.encode())))
        self.end_headers()
        self.wfile.write(body.encode())


if __name__ == '__main__':
    ThreadingHTTPServer(('127.0.0.1', 8765), Handler).serve_forever()
