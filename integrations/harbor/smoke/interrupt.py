"""Exercise benchmark cancellation against a real, synthetic Harbor container."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from threading import Event, Thread
import time

from harbor.models.job.config import JobConfig
from skua_harbor.agent import checked_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    bundle, _ = checked_bundle(args.bundle)
    spec = importlib.util.spec_from_file_location('benchmark_run', Path(__file__).parents[1] / 'benchmark/run.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    base = args.output.resolve()
    base.mkdir(parents=True, exist_ok=False)
    task = base / 'task'
    shutil.copytree(Path(__file__).parent / 'task', task)
    (task / 'instruction.md').write_text('Fixture mode: timeout\n')
    job = {'job_name': 'interrupted', 'jobs_dir': str(base / 'jobs'), 'n_concurrent_trials': 1,
           'n_attempts': 1, 'skip_credential_checks': True, 'retry': {'max_retries': 0},
           'environment': {'type': 'docker'}, 'tasks': [{'path': str(task)}],
           'agents': [{'import_path': 'skua_harbor.agent:SkuaAgent', 'model_name': 'openai/mock',
                       'base_url': 'http://127.0.0.1:8765/v1',
                       'model_api_format': 'openai_chat_completions',
                       'kwargs': {'bundle_path': str(bundle), 'auth_enabled': False,
                                  'max_seconds': 300, 'max_tool_calls': 5}}]}
    config_path = base / 'job.json'
    config_path.write_text(JobConfig.model_validate(job).model_dump_json(exclude_none=True, indent=2))
    controller = runner.StopController()
    finished = Event()
    observed = []
    job_dir = base / 'jobs/interrupted'

    def interrupt_when_running():
        from harbor.environments.docker.docker import _sanitize_docker_compose_project_name
        deadline = time.monotonic() + 180
        while not finished.wait(.5):
            for path in job_dir.glob('*/config.json'):
                project = _sanitize_docker_compose_project_name(path.parent.name + '__env')
                try:
                    ids = subprocess.check_output(['docker', 'ps', '-q', '--filter',
                        'label=com.docker.compose.project=' + project], text=True, timeout=5).split()
                except subprocess.SubprocessError:
                    continue
                if ids:
                    observed.append(project)
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
            if time.monotonic() >= deadline:
                controller.request(signal.SIGTERM)
                return

    controller.install()
    monitor = Thread(target=interrupt_when_running, daemon=True)
    monitor.start()
    try:
        with (base / 'harbor.log').open('w') as log:
            code = controller.run([str(Path(sys.executable).with_name('harbor')), 'run', '--config', str(config_path)],
                env=dict(os.environ, LITELLM_LOCAL_MODEL_COST_MAP='True'), stdout=log, stderr=subprocess.STDOUT)
    finally:
        finished.set()
        monitor.join(timeout=10)
        controller.restore()
        cleanup = runner.cleanup_interrupted_trial(job_dir, task)
    remaining = []
    for project in observed:
        for command in (['ps', '-aq'], ['network', 'ls', '-q'], ['volume', 'ls', '-q']):
            remaining += subprocess.check_output(['docker', *command, '--filter',
                'label=com.docker.compose.project=' + project], text=True, timeout=10).split()
    result = {'signal': controller.signum, 'observed_projects': observed, 'harbor_exit_code': code,
              'forced_stop': controller.forced, 'cleanup_failures': cleanup, 'remaining_resources': remaining}
    (base / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    assert observed and controller.signum == signal.SIGTERM and not cleanup and not remaining, result
    print('Real SIGTERM cancellation passed; trial artifacts retained, no owned containers/networks/volumes remain.')


if __name__ == '__main__':
    main()
