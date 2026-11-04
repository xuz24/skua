"""Uploaded into the container; imports Skua only, never Harbor."""
import argparse
import json
from pathlib import Path

from skua.config import build_config
from skua.headless import cli_run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--settings', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--logs', required=True)
    args = parser.parse_args()
    settings = json.loads(Path(args.settings).read_text())
    # Explicit runner config: task-local configuration cannot change the endpoint,
    # enable managed Git, or select a different authorization policy.
    config = build_config(Path(settings['root']).resolve(), [('harbor', {
        'schema_version': 1,
        'profiles': {'default': {'base_url': settings['base_url'], 'model': settings['model'],
                                'stream': True, 'auth': {'enabled': settings['auth_enabled'],
                                                       'env_var': 'SKUA_API_KEY'}}},
        'shell': {'executable': '/bin/bash'}, 'git': {'managed': False},
        # Runtime executables contain no provider credentials. Protect the input
        # files, not the entire runtime tree needed for task validation.
        'sensitive': {'filenames': ['settings.json', 'task.txt']},
    })])
    logs = Path(args.logs)
    return cli_run(config, argparse.Namespace(prompt_file=args.task, policy='container',
        managed_git='off', max_seconds=settings['max_seconds'], max_tool_calls=settings['max_tool_calls'],
        result_file=str(logs / 'result.json'), transcript_file=str(logs / 'session.jsonl')),
        task_key_directories=(config.root / 'ssl', Path('/etc/nginx/ssl')))


if __name__ == '__main__':
    raise SystemExit(main())
