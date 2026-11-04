"""Pinned, budget-limited local Terminal-Bench evaluation (Docker Desktop)."""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from threading import Thread
import time
import tomllib
import urllib.request

from harbor.models.job.config import JobConfig
from skua_harbor.agent import checked_bundle
from skua_harbor.budget_gateway import Gateway, Ledger

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from skua.config import load_config, resolve_root, resolve_credential
from skua.headless import positive_seconds

REVISION = '69671fbaac6d67a7ef0dfec016cc38a64ef7a77c'
ARCHIVE_SHA = 'afcd2b9813fc08e8d2b955db8165cc057acf0e37f74e55d26fc2506da196cebe'
LIMITS = {'diagnostic': .01, 'pilot': .60, 'pilot-runtime': .48, 'full': 13.72}
# Fixed from public requirements, not scores: dependency modernization, package
# service execution, and model inference with saved output.
PILOT_TASKS = ('modernize-scientific-stack', 'pypi-server', 'pytorch-model-cli')
EXCLUSIONS_PATH = Path(__file__).with_name('task-exclusions.json')

# Explicit image-digest scope; never infer a package-source workaround from scores.
GIT_BOOTSTRAP_IMAGES = {
    'alexgshaw/qemu-startup@sha256:1a666e049db553c5e713830b1f5de1953210ab7c99212d006bb4e46cbc6b6d04',
    'alexgshaw/qemu-alpine-ssh@sha256:541e8214a8c4faa7b556531b899d45da21048fa2ca6dd328d8cb6c4cffa8e964',
}


def git_bootstrap_profile(image):
    return 'bullseye-20251020' if image in GIT_BOOTSTRAP_IMAGES else 'default'


# Reviewed from public instruction.md files at REVISION, never solutions/tests.
# Keep programming tasks with explicit text specs even if they process image data.
VISUAL_TASKS = {
    'chess-best-move': 'Recover the board position from a PNG before choosing a move.',
    'code-from-image': 'Read pseudocode supplied only as an image.',
    'extract-moves-from-video': 'Transcribe commands visible in a gameplay video.',
    'financial-document-processor': 'Read and classify document contents in JPG/PDF inputs.',
    'gcode-to-text': 'Recognize the text formed by G-code toolpath geometry.',
    'path-tracing': 'Reconstruct a scene from a reference image.',
    'video-processing': 'Infer jump events and performance metrics from video content.',
}


def select_tasks(tasks, subset, phase, task_names=None):
    names = [t['name'] for t in tasks]
    if len(names) != 89 or len(set(names)) != 89 or any(t['git_commit_id'] != REVISION for t in tasks):
        raise SystemExit('Unexpected dataset revision or task count')
    if not VISUAL_TASKS.keys() <= set(names):
        raise SystemExit('Dataset does not match the reviewed visual-task exclusions')
    if subset not in {'reviewed-text', 'text-only', 'all'}:
        raise SystemExit('Unknown task subset')
    excluded = dict(sorted(VISUAL_TASKS.items())) if subset != 'all' else {}
    policy = None
    if subset == 'reviewed-text':
        raw = EXCLUSIONS_PATH.read_bytes()
        policy = json.loads(raw)
        if (policy['dataset_revision'] != REVISION or not policy['tasks'].keys() <= set(names)
                or any(not entry.get('reason') or not entry.get('sources') or not entry.get('evidence')
                       for entry in policy['tasks'].values())):
            raise SystemExit('Dataset does not match the reviewed task-issue exclusions')
        policy['sha256'] = hashlib.sha256(raw).hexdigest()
        for name, entry in policy['tasks'].items():
            excluded[name] = ' '.join(filter(None, [excluded.get(name), entry['reason']]))
        excluded = dict(sorted(excluded.items()))
    eligible = sorted((t for t in tasks if t['name'] not in excluded), key=lambda t: t['name'])
    by_name = {t['name']: t for t in eligible}
    if not set(PILOT_TASKS) <= by_name.keys():
        raise SystemExit('Dataset does not contain the fixed capability pilot')
    selected = [by_name[n] for n in PILOT_TASKS] if phase.startswith('pilot') else eligible if phase == 'full' else []
    if phase == 'rerun':
        if not task_names or len(set(task_names)) != len(task_names) or not set(task_names) <= by_name.keys():
            raise SystemExit('Rerun requires unique eligible task names')
        selected = [by_name[n] for n in sorted(task_names)]
    elif task_names:
        raise SystemExit('Explicit task names are only allowed for rerun')
    return selected, {'subset': subset, 'dataset_tasks': len(tasks), 'eligible_tasks': len(eligible),
                      'exclusion_policy': policy, 'excluded_tasks': excluded, 'selected_tasks': [t['name'] for t in selected]}


def check_pilot_selection(manifest, selection):
    # Old manifests described the all-task cohort, even though the pilot used three tasks.
    if (manifest.get('subset', 'all') != selection['subset'] or
            manifest.get('excluded_tasks', {}) != selection['excluded_tasks'] or
            manifest.get('exclusion_policy') != selection.get('exclusion_policy') or
            manifest.get('selected_tasks') != list(PILOT_TASKS)):
        raise SystemExit('Run and inspect a pilot with the same task subset before full evaluation')


def check_full_gate(diagnostic, pilot, pilot_manifest, selection, expected):
    check_pilot_selection(pilot_manifest, selection)
    for key, value in expected.items():
        if pilot_manifest.get(key) != value:
            raise SystemExit(f'Pilot configuration differs: {key}; run a matching pilot before full evaluation')
    if not diagnostic.get('native_tool_call'):
        raise SystemExit('Complete the native-tool connectivity diagnostic first')
    if (pilot_manifest.get('stopped_reason') != 'completed' or
            len(pilot) != len(PILOT_TASKS) or {r['task'] for r in pilot} != set(PILOT_TASKS) or
            any(r['category'] in {'infrastructure', 'policy', 'model', 'interrupted', 'cost_limit'} for r in pilot) or
            not any(r['category'] == 'passed' and (r.get('rewards') or {}).get('reward') == 1 for r in pilot)):
        raise SystemExit('Pilot gate failed: require all capability tasks, no infrastructure/policy/model/cost failures, '
                         'and at least one externally verified pass')


class StopController:
    """Stop scheduling; let Harbor cancel and clean up before a bounded hard stop."""
    def __init__(self, grace=60):
        self.signum = None
        self.grace = grace
        self.forced = False

    def request(self, signum, frame=None):
        if self.signum is None:
            self.signum = signum

    def install(self):
        self.previous = {sig: signal.signal(sig, self.request) for sig in (signal.SIGINT, signal.SIGTERM)}

    def restore(self):
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)

    def run(self, command, **kwargs):
        if self.signum:
            return None
        child = subprocess.Popen(command, start_new_session=True, **kwargs)
        deadline = None
        while True:
            if self.signum and deadline is None:
                deadline = time.monotonic() + self.grace
                try:
                    child.send_signal(signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if deadline is not None and time.monotonic() >= deadline:
                self.forced = True
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return child.wait()
            try:
                return child.wait(timeout=.25)
            except subprocess.TimeoutExpired:
                pass


def cleanup_interrupted_trial(job_dir, task_path):
    """Only touch Docker resources labelled for this run's recorded trial."""
    from harbor.environments.docker.docker import _sanitize_docker_compose_project_name
    failures = []
    for config_path in job_dir.glob('*/config.json'):
        config = json.loads(config_path.read_text())
        name = config.get('trial_name')
        if (name != config_path.parent.name or
                Path(config.get('trials_dir', '')).resolve() != job_dir.resolve() or
                Path(config.get('task', {}).get('path', '')).resolve() != task_path.resolve()):
            failures.append('trial_identity_mismatch')
            continue
        project = _sanitize_docker_compose_project_name(name + '__env')
        label = 'com.docker.compose.project=' + project
        for kind, listing, removal in (
                ('containers', ['ps', '-aq'], ['rm', '-f']),
                ('networks', ['network', 'ls', '-q'], ['network', 'rm']),
                ('volumes', ['volume', 'ls', '-q'], ['volume', 'rm'])):
            try:
                ids = subprocess.check_output(['docker', *listing, '--filter', 'label=' + label],
                                              text=True, timeout=30).split()
                if ids:
                    subprocess.run(['docker', *removal, *ids], check=True, timeout=60,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                failures.append(kind)
    return failures


def task_budget(phase, dataset_size, subset='reviewed-text'):
    # Explicitly authorized rates; further exclusions never redistribute allowances.
    if phase in {'full', 'rerun'}:
        return .28 if subset == 'reviewed-text' else .16
    return LIMITS[phase] / (1 if phase == 'diagnostic' else 3)


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')


def capture(command, **kwargs):
    return subprocess.check_output(command, text=True, timeout=120, **kwargs).strip()


def classify(trial, native, budget):
    if budget:
        return 'cost_limit'
    exception = (trial.get('exception_info') or {}).get('exception_type', '')
    if 'Setup' in exception or 'Build' in exception:
        return 'infrastructure'
    if native.get('reason') == 'time_limit' or 'Timeout' in exception:
        return 'timeout'
    if native.get('reason') == 'tool_limit':
        return 'tool_limit'
    if native.get('status') == 'approval_required':
        return 'policy'
    if native.get('reason') == 'unresolved_tool_error':
        return 'tool'
    rewards = (trial.get('verifier_result') or {}).get('rewards')
    if rewards is not None:
        return 'passed' if rewards.get('reward') == 1 else 'task'
    if native.get('reason') == 'turn_error':
        return 'model'
    return 'infrastructure'


def secondary_flags(events, records):
    """Observed signals, not exclusive causes or a claim errors stayed unresolved."""
    flags = set()
    for event in events:
        if event.get('code') == 'model_refusal':
            flags.add('model_refusal')
        if event.get('type') != 'tool_result':
            continue
        if (event.get('error') or {}).get('code') == 'invalid_arguments':
            flags.add('invalid_arguments')
        text = event.get('model_text', '')
        if event.get('status') == 'error':
            if any(s in text for s in ('ModuleNotFoundError', 'command not found', 'No module named')):
                flags.add('missing_dependency_observed')
            if 'Split patches by repository' in text:
                flags.add('patch_repository_restriction')
    for record in records:
        reason = record.get('finish_reason')
        if reason == 'length':
            flags.add('response_truncated')
        elif reason == 'content_filter':
            flags.add('content_filtered')
    return sorted(flags)


def read_outcome(path, label, flags):
    """Read optional artifacts without letting their failure erase other evidence."""
    try:
        raw = path.read_text()
    except FileNotFoundError:
        flags.add(label + '_missing')
        return {}
    except (OSError, UnicodeError):
        flags.add(label + '_unreadable')
        return {}
    if not raw.strip():
        flags.add(label + '_empty')
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        flags.add(label + '_invalid')
        return {}
    if not isinstance(value, dict):
        flags.add(label + '_invalid')
        return {}
    return value


def collect_outcome(output, name, returncode, records, denials):
    flags = set()
    trials = sorted((output / 'jobs' / name).glob('*/result.json'))
    row = {'task': name, 'harbor_exit_code': returncode, 'trial': None}
    trial, native, events = {}, {}, []
    if len(trials) != 1:
        flags.add('trial_result_missing' if not trials else 'trial_result_ambiguous')
    else:
        path = trials[0]
        row['trial'] = str(path.parent.relative_to(output))
        trial = read_outcome(path, 'trial_result', flags)
        # Validate the fields consumed below independently, retaining valid rewards
        # even if another field is malformed.
        for key in ('exception_info', 'verifier_result'):
            if trial.get(key) is not None and not isinstance(trial[key], dict):
                flags.add('trial_result_invalid')
                trial.pop(key)
        exception = trial.get('exception_info') or {}
        if exception.get('exception_type') is not None and not isinstance(exception['exception_type'], str):
            flags.add('trial_result_invalid')
            exception.pop('exception_type')
        verifier = trial.get('verifier_result') or {}
        if verifier.get('rewards') is not None and not isinstance(verifier['rewards'], dict):
            flags.add('trial_result_invalid')
            verifier.pop('rewards')
        native = read_outcome(path.parent / 'agent/result.json', 'native_result', flags)
        for key in ('status', 'reason'):
            if native.get(key) is not None and not isinstance(native[key], str):
                flags.add('native_result_invalid')
                native.pop(key)
        session_path = path.parent / 'agent/session.jsonl'
        try:
            lines = session_path.read_text().splitlines()
            if not any(line.strip() for line in lines):
                flags.add('transcript_empty')
            for line in lines:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError('event must be an object')
                    # secondary_flags expects structured errors and textual output.
                    if (event.get('error') is not None and not isinstance(event['error'], dict)
                            or not isinstance(event.get('model_text', ''), str)):
                        raise ValueError('invalid event fields')
                    events.append(event)
                except ValueError:
                    flags.add('transcript_unreadable')
        except FileNotFoundError:
            flags.add('transcript_missing')
        except (OSError, UnicodeError):
            flags.add('transcript_unreadable')
    route = f'/v1/{name}'
    flags.update(secondary_flags(events, [r for r in records if r['route'] == route]))
    denied = any(d['route'] == route for d in denials)
    row.update(category=classify(trial, native, denied), native_status=native.get('status'),
               native_reason=native.get('reason'),
               exception_type=(trial.get('exception_info') or {}).get('exception_type'),
               rewards=(trial.get('verifier_result') or {}).get('rewards'),
               secondary_flags=sorted(flags))
    return row


def report(output, rows, ledger, *, manifest=None):
    manifest = manifest or {}
    write(output / 'results.json', rows)
    counts = Counter(row['category'] for row in rows)
    total = len(rows)
    passed = sum((row.get('rewards') or {}).get('reward') == 1 for row in rows)
    verified = sum((row.get('rewards') or {}).get('reward') in (0, 1) for row in rows)
    planned = len(manifest['selected_tasks']) if 'selected_tasks' in manifest else None
    accounted = sum(r['charged_usd'] for r in ledger.records)
    estimated = sum(r['estimated_actual_usd'] or 0 for r in ledger.records)
    unknown_usage = sum(r['estimated_actual_usd'] is None for r in ledger.records)
    summary = {'recorded_tasks': total, 'planned_tasks': planned, 'verified_passes': passed,
               'unrecorded_tasks': max(0, planned - total) if planned is not None else None,
               'verified_tasks': verified, 'unverified_tasks': total - verified,
               'categories': dict(sorted(counts.items())), 'accounted_usd': accounted,
               'estimated_usage_cost_usd': estimated, 'requests_without_usage': unknown_usage,
               'budget_limit_usd': ledger.limit, 'stopped_reason': manifest.get('stopped_reason')}
    summary.update({key: manifest.get(key) for key in ('subset', 'dataset_tasks', 'eligible_tasks')})
    summary['excluded_tasks'] = manifest.get('excluded_tasks', {})
    summary['exclusion_policy'] = manifest.get('exclusion_policy')
    summary['secondary_flags'] = dict(sorted(Counter(flag for r in rows for flag in r.get('secondary_flags', [])).items()))
    write(output / 'summary.json', summary)
    def cell(value):
        return str(value if value is not None else '—').replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
    coverage = f'{total}/{planned} selected tasks have recorded outcomes' if planned is not None else f'{total} task outcomes recorded; planned count unknown'
    limits = ', '.join(f'{label}: {manifest[key]}' for key, label in [
        ('max_seconds', 'agent seconds'), ('max_tool_calls', 'tool calls'),
        ('max_completion_tokens', 'completion tokens/request'), ('platform', 'platform')]
        if key in manifest) or 'See manifest.json and per-task job configurations.'
    (output / 'report.md').write_text(
        f'# Terminal-Bench run: {output.name}\n\n'
        f'{coverage}. **{passed} externally verified passes.** '
        'Agent completion alone does not establish task success. '
        'Outcome categories retain agent failures even when external verification passes.\n\n'
        'One attempt per task; no retries. Partial coverage is not a full benchmark score '
        'or a representative estimate. This is not a leaderboard submission.\n\n'
        f'Subset: {cell(manifest.get("subset", "not recorded"))}. '
        'Subset results are not full-dataset scores.\n\n' +
        ('| Excluded task | Reason |\n|---|---|\n' + ''.join(
            f'| {cell(name)} | {cell(reason)} |\n' for name, reason in sorted(summary['excluded_tasks'].items())) + '\n'
            if summary['excluded_tasks'] else '') +
        ('Reviewed issue sources (reports are not all locally reproduced):\n\n' + ''.join(
            f'- {cell(name)} ({cell(entry["evidence"])}): ' +
            ', '.join(f'[source {i + 1}]({url})' for i, url in enumerate(entry['sources'])) + '\n'
            for name, entry in summary['exclusion_policy']['tasks'].items()) + '\n'
            if summary['exclusion_policy'] else '') +
        f'Stop reason: {cell(manifest.get("stopped_reason", "not recorded"))}.\n\n'
        '| Outcome category | Recorded tasks |\n|---|---:|\n' +
        ''.join(f'| {cell(category)} | {count} |\n' for category, count in sorted(counts.items())) +
        f'\nAccounted budget: ${accounted:.6f} / ${ledger.limit:.2f}. '
        f'Estimated cost from reported usage: ${estimated:.6f}. '
        f'Requests without usage: {unknown_usage}; their reservations remain accounted. '
        'Published-rate estimates, not provider billing.\n\n'
        f'Configuration: {limits}.\n\n'
        '| Task | Category | Agent status | Reason | Verifier reward | Trial artifacts | Secondary flags |\n|---|---|---|---|---|---|---|\n' +
        ''.join('| ' + ' | '.join(cell(value) for value in [r['task'], r['category'],
            r.get('native_status'), r.get('native_reason') or r.get('exception_type') or r.get('harness_exception_type'),
            (r.get('rewards') or {}).get('reward'), r.get('trial'), ', '.join(r.get('secondary_flags', []))]) + ' |\n' for r in rows))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=(*LIMITS, 'rerun'), required=True)
    parser.add_argument('--tasks', nargs='+', help='Explicit eligible task names, required for rerun only.')
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--subset', choices=('reviewed-text', 'text-only', 'all'), default='reviewed-text',
                        help='Default reviewed-text excludes visual tasks and documented issues; text-only/all restore unfiltered cohorts.')
    parser.add_argument('--list-tasks', action='store_true',
                        help='Print selection and exclusions without credentials, Docker, or model requests.')
    parser.add_argument('--max-seconds', type=positive_seconds, default=120,
                        help='Explicit task time ceiling in seconds (default: 120); dollar caps are unchanged.')
    args = parser.parse_args(argv)
    if (args.phase == 'rerun') != bool(args.tasks):
        parser.error('--tasks is required for rerun and forbidden for other phases')
    if not args.list_tasks and args.bundle is None:
        parser.error('--bundle is required unless --list-tasks is used')
    return args


def agent_config(bundle, model, endpoint, max_seconds, git_bootstrap='default', qemu_runtime_check=False):
    seconds = positive_seconds(max_seconds)
    return {'import_path': 'skua_harbor.agent:SkuaAgent', 'model_name': model,
            'skip_model_parsing': True, 'model_api_key_name': 'SKUA_API_KEY',
            'base_url': endpoint, 'model_api_format': 'openai_chat_completions',
            'override_timeout_sec': math.ceil(seconds + 90), 'override_setup_timeout_sec': 600,
            'kwargs': {'bundle_path': str(bundle), 'max_seconds': seconds, 'max_tool_calls': 40,
                       'git_bootstrap': git_bootstrap, 'qemu_runtime_check': qemu_runtime_check}}


def main():
    args = parse_args()
    data = args.data.resolve()
    if hashlib.sha256((data / 'dataset.tar.gz').read_bytes()).hexdigest() != ARCHIVE_SHA:
        raise SystemExit('Dataset archive hash mismatch')
    dataset = json.loads((data / 'dataset.json').read_text())
    tasks = dataset['tasks']
    selected, selection = select_tasks(tasks, args.subset, args.phase, args.tasks)
    if args.list_tasks:
        print(json.dumps(selection, indent=2))
        return
    bundle, bundle_manifest = checked_bundle(args.bundle)
    config = load_config(resolve_root(None))
    profile = config.profile()
    credential = resolve_credential(config)
    if not credential or profile.model != 'gpt-5.6-luna':
        raise SystemExit('Expected configured gpt-5.6-luna and credential')
    source_hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / 'integrations/harbor').rglob('*.py') if '__pycache__' not in str(p)}
    if args.phase == 'full':
        check_full_gate(json.loads((data / 'diagnostic/diagnostic.json').read_text()),
            json.loads((data / 'pilot-runtime/results.json').read_text()),
            json.loads((data / 'pilot-runtime/manifest.json').read_text()), selection,
            {'max_seconds': args.max_seconds, 'bundle': bundle_manifest,
             'adapter_files_sha256': source_hashes, 'model': profile.model, 'endpoint': profile.base_url,
             'platform': 'linux/amd64', 'max_tool_calls': 40, 'max_completion_tokens': 8192})
    if args.phase == 'rerun':
        diagnostic = json.loads((data / 'diagnostic/diagnostic.json').read_text())
        if not diagnostic.get('native_tool_call'):
            raise SystemExit('Complete the connectivity diagnostic before a focused rerun')
    output = data / args.phase
    output.mkdir(exist_ok=False)  # Never reset/re-spend an existing phase budget.
    snapshot = output / 'harness-source'
    shutil.copytree(ROOT / 'integrations/harbor', snapshot,
                    ignore=shutil.ignore_patterns('__pycache__', '*.egg-info', 'build'))
    route_limit = task_budget(args.phase, len(tasks), args.subset)
    routes = {f'/v1/{t["name"]}': route_limit for t in selected}
    if args.phase == 'diagnostic':
        routes = {'/v1/diagnostic': .01}
    phase_limit = (round(route_limit * len(selected), 8) if args.phase == 'rerun' else LIMITS[args.phase])
    ledger = Ledger(output / 'budget.json', total_limit=phase_limit, route_limits=routes)
    gateway = Gateway(ledger, endpoint=profile.base_url, model=profile.model, credential=credential,
                      max_completion_tokens=1024 if args.phase == 'diagnostic' else 8192)
    thread = Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ, SKUA_API_KEY=gateway.token, LITELLM_LOCAL_MODEL_COST_MAP='True',
               DOCKER_DEFAULT_PLATFORM='linux/amd64')
    # Never pass the provider credential (including its custom variable) to Harbor.
    if profile.auth.env_var != 'SKUA_API_KEY':
        env.pop(profile.auth.env_var, None)
    port = gateway.server_address[1]
    manifest = {'dataset': {'name': 'terminal-bench', 'version': '2.0', 'revision': REVISION,
                    'archive_sha256': ARCHIVE_SHA}, 'harbor': '0.25.0', 'bundle': bundle_manifest,
                'skua_git_head': capture(['git', 'rev-parse', 'HEAD'], cwd=ROOT),
                'skua_dirty': bool(capture(['git', 'status', '--porcelain'], cwd=ROOT)),
                'adapter_files_sha256': source_hashes,
                'model': profile.model, 'endpoint': profile.base_url,
                'phase': args.phase, 'selected_tasks': [t['name'] for t in selected],
                'budget_usd': phase_limit, 'all_phases_budget_usd': sum(LIMITS.values()),
                'input_per_million_usd': .20, 'output_per_million_usd': 1.20,
                'pricing_source': 'https://www.its.umich.edu/computing/ai/pricing',
                'started': time.time(), 'repetitions': 1, 'max_seconds': args.max_seconds, 'max_tool_calls': 40,
                'max_completion_tokens': gateway.completion_limit, 'platform': 'linux/amd64',
                'streaming': 'buffered upstream nonstreaming, downstream SSE', 'images': {}}
    manifest.update(selection)
    manifest['per_task_budget_usd'] = route_limit
    write(output / 'manifest.json', manifest)
    rows = []
    stop = StopController()
    stop.install()
    try:
        if args.phase == 'diagnostic':
            body = {'model': profile.model, 'messages': [{'role': 'user', 'content': 'Call the ping function with value ok.'}],
                'tools': [{'type': 'function', 'function': {'name': 'ping', 'description': 'Connectivity test',
                    'parameters': {'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']}}}],
                'tool_choice': {'type': 'function', 'function': {'name': 'ping'}}, 'stream': False}
            req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/diagnostic/chat/completions',
                data=json.dumps(body).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + gateway.token})
            with urllib.request.urlopen(req, timeout=100) as response:
                reply = json.load(response)
            calls = reply['choices'][0]['message'].get('tool_calls', [])
            write(output / 'diagnostic.json', {'native_tool_call': bool(calls), 'usage': reply.get('usage')})
            print('Diagnostic:', bool(calls), 'usage:', reply.get('usage'), flush=True)
            if not calls:
                raise RuntimeError('Model did not produce a native tool call')
            manifest['stopped_reason'] = 'completed'
            return
        for number, task in enumerate(selected, 1):
            if stop.signum:
                break
            if shutil.disk_usage(data).free < 5 * 1024**3:
                manifest['stopped_reason'] = 'host_disk_below_5_gib'
                break
            name = task['name']
            print(f'{args.phase} {number}/{len(selected)}: {name}', flush=True)
            row = {'task': name, 'category': 'infrastructure'}
            pinned = None
            existed = True
            try:
                source = data / 'source' / ('terminal-bench-2-' + REVISION) / task['path']
                target = output / 'tasks' / name
                shutil.copytree(source, target)
                task_file = target / 'task.toml'
                settings = tomllib.loads(task_file.read_text())
                image = settings['environment']['docker_image']
                descriptor = json.loads(capture(['docker', 'manifest', 'inspect', '--verbose', image]))
                if isinstance(descriptor, list):
                    descriptor = next(d for d in descriptor if d['Descriptor']['platform']['architecture'] == 'amd64')
                digest = descriptor['Descriptor']['digest']
                pinned = image.split(':')[0] + '@' + digest
                existed = subprocess.run(['docker', 'image', 'inspect', pinned], stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL).returncode == 0
                task_file.write_text(task_file.read_text().replace(image, pinned))
                manifest['images'][name] = {'source': image, 'pinned': pinned, 'platform': descriptor['Descriptor'].get('platform'),
                                            'git_bootstrap': git_bootstrap_profile(pinned)}
                write(output / 'manifest.json', manifest)
                job = {'job_name': name, 'jobs_dir': str(output / 'jobs'), 'n_concurrent_trials': 1,
                    'n_attempts': 1, 'skip_credential_checks': True, 'retry': {'max_retries': 0},
                    'environment': {'type': 'docker'}, 'verifier': {'max_timeout_sec': 300},
                    'agents': [agent_config(bundle, profile.model,
                        f'http://host.docker.internal:{port}/v1/{name}', args.max_seconds, git_bootstrap_profile(pinned), pinned in GIT_BOOTSTRAP_IMAGES)],
                    'tasks': [{'path': str(target)}]}
                job_path = output / (name + '.json')
                job_path.write_text(JobConfig.model_validate(job).model_dump_json(exclude_none=True, indent=2))
                with (output / (name + '.log')).open('w') as log:
                    returncode = stop.run([str(Path(sys.executable).with_name('harbor')), 'run', '--config', str(job_path)],
                        env=env, stdout=log, stderr=subprocess.STDOUT)
                row.update(collect_outcome(output, name, returncode, ledger.records, ledger.denials))
            except Exception as exc:
                row['harness_exception_type'] = type(exc).__name__
            finally:
                if stop.signum:
                    row.update(category='interrupted', native_reason='signal',
                               signal=signal.Signals(stop.signum).name, forced_stop=stop.forced)
                    try:
                        row['cleanup_failures'] = cleanup_interrupted_trial(output / 'jobs' / name, output / 'tasks' / name)
                    except Exception:
                        row['cleanup_failures'] = ['cleanup_error']
                if pinned and not existed:
                    # Remove only this run's newly fetched image, without forcing
                    # deletion of in-use containers or touching pre-existing images.
                    try:
                        subprocess.run(['docker', 'image', 'rm', pinned], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=30)
                    except (OSError, subprocess.TimeoutExpired):
                        row['image_cleanup_failed'] = True
            rows.append(row)
            report(output, rows, ledger, manifest=manifest)
            print(json.dumps(row), flush=True)
            if ledger.halted:
                manifest['stopped_reason'] = 'usage_bound_exceeded'
                break
    finally:
        manifest['stopped_reason'] = ('signal' if stop.signum else manifest.get('stopped_reason',
            'completed' if args.phase != 'diagnostic' and len(rows) == len(selected) else 'incomplete'))
        if stop.signum:
            manifest['signal'] = signal.Signals(stop.signum).name
        gateway.shutdown()
        gateway.server_close()
        manifest['finished'] = time.time()
        write(output / 'manifest.json', manifest)
        if args.phase != 'diagnostic':
            report(output, rows, ledger, manifest=manifest)
        stop.restore()
        if stop.signum:
            raise SystemExit(128 + stop.signum)


if __name__ == '__main__':
    main()
