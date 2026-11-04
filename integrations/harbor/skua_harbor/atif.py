"""Export native Skua history using Harbor 0.25.0's actual ATIF validator."""
import json
from pathlib import Path

from harbor.models.trajectories import Trajectory


def export_trajectory(path: Path, *, wheel_sha256: str) -> Trajectory:
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not records or records[0].get('type') != 'session' or records[0].get('schema_version') != 1:
        raise ValueError('Expected a Skua v1 transcript')
    header, *events = records
    by_seq = {e['seq']: e for e in events}
    if len(by_seq) != len(events) or len(events) != header['event_count']:
        raise ValueError('Incomplete or duplicate native history')
    steps = []
    call_steps = {}
    completed = set()
    for event in events:
        if event['type'] == 'message':
            step = {'step_id': len(steps) + 1, 'timestamp': event['at'],
                    'source': 'agent' if event['role'] == 'assistant' else event['role'],
                    'message': event['text'], 'extra': {'completion': event['completion']}}
            calls = []
            for seq in event['call_seqs']:
                call = by_seq[seq]
                if call['type'] != 'tool_call' or seq in call_steps:
                    raise ValueError('Invalid native tool reference')
                calls.append({'tool_call_id': f'skua_call_{seq}', 'function_name': call['name'],
                              'arguments': call['arguments']})
                call_steps[seq] = step
            if calls:
                step['tool_calls'] = calls
            steps.append(step)
        elif event['type'] == 'tool_result':
            seq = event['call_seq']
            if seq not in call_steps or seq in completed:
                raise ValueError('Invalid native tool result')
            completed.add(seq)
            observation = call_steps[seq].setdefault('observation', {'results': []})
            observation['results'].append({'source_call_id': f'skua_call_{seq}',
                'content': event['model_text'], 'extra': {'status': event['status']}})
    if completed != set(call_steps):
        raise ValueError('Native history has unresolved tool calls')
    document = {'schema_version': 'ATIF-v1.8', 'session_id': header['session_id'],
        'agent': {'name': 'skua', 'version': 'sha256:' + wheel_sha256}, 'steps': steps,
        'notes': 'Native conversation only; hidden reasoning and provider token/cost usage are unavailable.',
        'extra': {'provider_usage': None, 'task_success_verified': False,
                  'turn_outcomes': [{'status': e['status'], 'reason': e['reason']} for e in events if e['type'] == 'turn_end']}}
    # Omit unavailable metrics instead of manufacturing zero usage or costs.
    return Trajectory.model_validate(document)
