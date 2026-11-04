"""Run real Docker/Harbor trials against a synthetic in-container endpoint."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from harbor.models.job.config import JobConfig
from harbor.models.trajectories import Trajectory
from skua_harbor.agent import checked_bundle


def job_config(base, bundle, mode):
    return {'job_name': 'skua-' + mode, 'jobs_dir': str(base / 'jobs'),
        'n_concurrent_trials': 1, 'n_attempts': 1, 'skip_credential_checks': True,
        'retry': {'max_retries': 0}, 'environment': {'type': 'docker'},
        'agents': [{'import_path': 'skua_harbor.agent:SkuaAgent', 'model_name': 'openai/mock',
                    'base_url': 'http://127.0.0.1:8765/v1', 'model_api_format': 'openai_chat_completions',
                    'kwargs': {'bundle_path': str(bundle), 'auth_enabled': False,
                               'max_seconds': 2 if mode == 'timeout' else 30, 'max_tool_calls': 5}}],
        'tasks': [{'path': str(base / ('task-' + mode))}]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='new directory for tasks, configs, and trial artifacts')
    parser.add_argument('--prepare-only', action='store_true', help='validate pinned API/config without claiming container execution')
    args = parser.parse_args()
    bundle, _ = checked_bundle(args.bundle)
    base = args.output.resolve()
    base.mkdir(parents=True, exist_ok=False)
    env = dict(os.environ, LITELLM_LOCAL_MODEL_COST_MAP='True')
    source = Path(__file__).parent / 'task'
    for mode in ('success', 'failure', 'verified-tool-failure', 'certificate-key', 'timeout'):
        target = base / ('task-' + mode)
        shutil.copytree(source, target)
        (target / 'instruction.md').write_text(f'Fixture mode: {mode}\nCreate answer.txt containing harbor-ready and a newline.\n')
        config = job_config(base, bundle, mode)
        parsed = JobConfig.model_validate(config)
        path = base / (mode + '.json')
        path.write_text(parsed.model_dump_json(exclude_none=True, indent=2) + '\n')
        if args.prepare_only:
            continue
        if not shutil.which('docker'):
            raise SystemExit('Docker is required. Prepared configs remain in the output directory.')
        subprocess.run(['docker', 'info'], check=True, capture_output=True)
        completed = subprocess.run([str(Path(sys.executable).with_name('harbor')), 'run', '--config', str(path)], env=env)
        job = base / 'jobs' / ('skua-' + mode)
        outcomes = list(job.glob('*/agent/result.json'))
        if len(outcomes) != 1:
            raise RuntimeError(f'Expected one retained Skua result for {mode}; inspect {job}')
        outcome = json.loads(outcomes[0].read_text())
        expected = {'success': 'completed', 'failure': 'failed', 'verified-tool-failure': 'failed', 'certificate-key': 'completed', 'timeout': 'budget_exhausted'}[mode]
        assert outcome['status'] == expected, outcome
        native = outcomes[0].with_name('session.jsonl')
        assert native.stat().st_size
        Trajectory.model_validate_json(outcomes[0].with_name('trajectory.json').read_text())
        if mode in {'success', 'failure', 'verified-tool-failure', 'certificate-key'}:
            assert completed.returncode == 0
            reward = outcomes[0].parents[1] / 'verifier/reward.txt'
            expected_reward = '0' if mode == 'failure' else '1'
            assert reward.read_text().strip() == expected_reward
            trial = json.loads((outcomes[0].parents[1] / 'result.json').read_text())
            assert trial['exception_info'] is None
            if mode in {'failure', 'verified-tool-failure'}:
                assert outcome['reason'] == 'unresolved_tool_error'
                assert outcome['task_success_verified'] is False
    print('Prepared and schema-validated only; Docker not exercised.' if args.prepare_only else
          'Five Docker trials passed: success, verified failure, verified tool-error success, task key creation, timeout artifacts.')


if __name__ == '__main__':
    main()
