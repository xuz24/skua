"""Reports keep run coverage, verifier results, and cost uncertainty distinct."""
import importlib.util
import json
from pathlib import Path
import pytest

from skua_harbor.budget_gateway import Ledger


def load_runner():
    path = Path(__file__).parents[1] / 'benchmark/run.py'
    spec = importlib.util.spec_from_file_location('benchmark_reporting', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_partial_report_distinguishes_completion_verification_and_unknown_usage(tmp_path):
    runner = load_runner()
    ledger = Ledger(tmp_path / 'budget.json', total_limit=1, route_limits={'task': 1})
    ledger.reserve('task', {'messages': []}, 10)
    rows = [dict(task='task|one', category='task', native_status='completed',
                 native_reason='turn_completed', rewards={'reward': 0}, trial='jobs/one')]
    runner.report(tmp_path, rows, ledger, manifest={'selected_tasks': ['one', 'two', 'three'],
        'max_seconds': 240, 'max_tool_calls': 50, 'max_completion_tokens': 1024,
        'platform': 'linux/arm64', 'stopped_reason': 'user_requested'})
    summary = json.loads((tmp_path / 'summary.json').read_text())
    assert summary['recorded_tasks'] == 1 and summary['unrecorded_tasks'] == 2
    assert summary['verified_passes'] == 0 and summary['requests_without_usage'] == 1
    assert summary['accounted_usd'] > summary['estimated_usage_cost_usd'] == 0
    report = (tmp_path / 'report.md').read_text()
    assert '1/3 selected tasks' in report and '0 externally verified passes' in report
    assert 'agent seconds: 240' in report and '120 agent seconds' not in report
    assert 'task\\|one' in report and 'turn_completed | 0 |' in report
    assert 'user_requested' in report and 'None' not in report
    assert json.loads((tmp_path / 'results.json').read_text()) == rows


def test_empty_report_does_not_invent_coverage_or_limits(tmp_path):
    ledger = Ledger(tmp_path / 'budget.json', total_limit=1, route_limits={})
    load_runner().report(tmp_path, [], ledger)
    summary = json.loads((tmp_path / 'summary.json').read_text())
    assert summary['planned_tasks'] is None and summary['unrecorded_tasks'] is None
    assert 'planned count unknown' in (tmp_path / 'report.md').read_text()


def test_explicit_time_ceiling_reaches_agent_and_outer_timeout(tmp_path):
    from harbor.models.trial.config import AgentConfig
    runner = load_runner()
    arguments = ['--phase', 'pilot-runtime', '--data', str(tmp_path), '--bundle', str(tmp_path)]
    assert runner.parse_args(arguments).max_seconds == 120
    selected = runner.parse_args([*arguments, '--max-seconds', '300.5'])
    configured = AgentConfig.model_validate(runner.agent_config(tmp_path, 'test-model',
        'https://mock.invalid/v1', selected.max_seconds))
    assert configured.kwargs['max_seconds'] == 300.5
    assert configured.kwargs['max_tool_calls'] == 40
    assert configured.override_timeout_sec == 391
    assert configured.override_setup_timeout_sec == 600
    assert runner.LIMITS['pilot-runtime'] == .48


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf'])
def test_invalid_task_time_ceiling_rejected_before_run(value):
    with pytest.raises(SystemExit) as exc:
        load_runner().parse_args(['--phase', 'pilot-runtime', '--data', '/unused',
            '--bundle', '/unused', '--max-seconds', value])
    assert exc.value.code == 2


def fixture_tasks(runner):
    names = [*runner.VISUAL_TASKS, 'caffe-cifar-10', 'build-pov-ray', 'sam-cell-seg',
             'pytorch-model-cli', 'qemu-startup', 'build-cython-ext', 'configure-git-webserver']
    names = sorted(set(names) | set(runner.PILOT_TASKS) | set(json.loads(runner.EXCLUSIONS_PATH.read_text())['tasks']))
    names += [f'text-{i:03}' for i in range(89 - len(names))]
    return [{'name': name, 'git_commit_id': runner.REVISION} for name in reversed(names)]


def test_text_subset_is_explicit_deterministic_and_keeps_programmatic_image_tasks():
    runner = load_runner()
    tasks = fixture_tasks(runner)
    selected, selection = runner.select_tasks(tasks, 'text-only', 'full')
    names = [t['name'] for t in selected]
    assert names == sorted(names) and len(names) == 82
    assert selection['excluded_tasks'] == runner.VISUAL_TASKS
    assert not set(names) & runner.VISUAL_TASKS.keys()
    assert {'caffe-cifar-10', 'build-pov-ray', 'sam-cell-seg', 'pytorch-model-cli', 'qemu-startup'} <= set(names)
    assert runner.select_tasks(tasks, 'all', 'full')[1]['excluded_tasks'] == {}
    assert len(runner.select_tasks(tasks, 'all', 'full')[0]) == 89
    assert runner.select_tasks(tasks, 'text-only', 'pilot-runtime')[1]['selected_tasks'] == list(runner.PILOT_TASKS)


def test_subset_pilot_gate_rejects_different_or_legacy_cohort():
    runner = load_runner()
    tasks = fixture_tasks(runner)
    _, full = runner.select_tasks(tasks, 'text-only', 'full')
    _, pilot = runner.select_tasks(tasks, 'text-only', 'pilot-runtime')
    runner.check_pilot_selection(pilot, full)
    for bad in ({**pilot, 'subset': 'all'}, {**pilot, 'excluded_tasks': {}},
                {**pilot, 'selected_tasks': ['wrong']}, {'selected_tasks': pilot['selected_tasks']}):
        with pytest.raises(SystemExit, match='same task subset'):
            runner.check_pilot_selection(bad, full)


def test_subset_fails_closed_on_changed_dataset():
    runner = load_runner()
    tasks = fixture_tasks(runner)
    for bad in (tasks[:-1], [tasks[0], *tasks[:-1]],
                [{**t, 'git_commit_id': 'changed'} for t in tasks],
                [{**t, 'name': 'renamed'} if t['name'] == 'code-from-image' else t for t in tasks]):
        with pytest.raises(SystemExit):
            runner.select_tasks(bad, 'text-only', 'full')


def test_list_tasks_exits_before_credentials_bundle_pilot_or_execution(tmp_path, monkeypatch, capsys):
    runner = load_runner()
    import hashlib
    archive = b'fixture archive'
    (tmp_path / 'dataset.tar.gz').write_bytes(archive)
    monkeypatch.setattr(runner, 'ARCHIVE_SHA', hashlib.sha256(archive).hexdigest())
    (tmp_path / 'dataset.json').write_text(json.dumps({'tasks': fixture_tasks(runner)}))
    monkeypatch.setattr('sys.argv', ['run.py', '--phase', 'full', '--data', str(tmp_path), '--list-tasks'])
    def forbidden(*args, **kwargs):
        pytest.fail('Preview must not load credentials, validate a bundle, or execute commands')
    for name in ('load_config', 'checked_bundle', 'capture', 'Gateway'):
        monkeypatch.setattr(runner, name, forbidden)
    runner.main()
    plan = json.loads(capsys.readouterr().out)
    assert plan['subset'] == 'reviewed-text' and len(plan['selected_tasks']) == 49
    assert plan['exclusion_policy']['tasks']['configure-git-webserver']['sources']
    assert not (tmp_path / 'full').exists()


def test_report_separates_exclusions_from_unrecorded_tasks(tmp_path):
    runner = load_runner()
    _, selection = runner.select_tasks(fixture_tasks(runner), 'text-only', 'full')
    ledger = Ledger(tmp_path / 'budget.json', total_limit=14.24, route_limits={})
    runner.report(tmp_path, [], ledger, manifest=selection)
    summary = json.loads((tmp_path / 'summary.json').read_text())
    assert summary['planned_tasks'] == summary['eligible_tasks'] == summary['unrecorded_tasks'] == 82
    assert summary['dataset_tasks'] == 89 and len(summary['excluded_tasks']) == 7
    text = (tmp_path / 'report.md').read_text()
    assert 'Subset: text-only' in text and 'Subset results are not full-dataset scores' in text
    assert all(name in text and reason in text for name, reason in runner.VISUAL_TASKS.items())


def test_exclusions_leave_budget_unused_instead_of_redistributing_it():
    runner = load_runner()
    tasks = fixture_tasks(runner)
    per_task = runner.task_budget('full', len(tasks), 'text-only')
    assert per_task == pytest.approx(.16)
    selected, _ = runner.select_tasks(tasks, 'text-only', 'full')
    assert len(selected) * per_task == pytest.approx(13.12)
    assert len(selected) * per_task < runner.LIMITS['full']
    assert runner.task_budget('pilot-runtime', 89) * 3 == pytest.approx(.48)
    assert runner.task_budget('diagnostic', 89) == .01


def test_reviewed_exclusions_provenance_gate_and_budget(tmp_path, monkeypatch):
    runner = load_runner()
    tasks = fixture_tasks(runner)
    selected, full = runner.select_tasks(tasks, 'reviewed-text', 'full')
    _, pilot = runner.select_tasks(tasks, 'reviewed-text', 'pilot-runtime')
    assert len(selected) == 49
    assert len(full['excluded_tasks']) == 40  # Visual/issue overlap counted once.
    assert {'configure-git-webserver', 'build-cython-ext'} <= full['excluded_tasks'].keys()
    assert not set(runner.PILOT_TASKS) & full['excluded_tasks'].keys()
    assert len(selected) * runner.task_budget('full', 89) == pytest.approx(13.72)
    runner.check_pilot_selection(pilot, full)
    policy = json.loads(runner.EXCLUSIONS_PATH.read_text())
    policy['tasks']['build-cython-ext']['sources'].append('https://example.org/new-evidence')
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps(policy))
    monkeypatch.setattr(runner, 'EXCLUSIONS_PATH', path)
    _, changed = runner.select_tasks(tasks, 'reviewed-text', 'full')
    with pytest.raises(SystemExit, match='same task subset'):
        runner.check_pilot_selection(pilot, changed)
    policy['tasks']['unknown-task'] = policy['tasks']['build-cython-ext']
    path.write_text(json.dumps(policy))
    with pytest.raises(SystemExit, match='task-issue exclusions'):
        runner.select_tasks(tasks, 'reviewed-text', 'full')
    ledger = Ledger(tmp_path / 'budget.json', total_limit=14.24, route_limits={})
    runner.report(tmp_path, [], ledger, manifest=full)
    summary = json.loads((tmp_path / 'summary.json').read_text())
    assert summary['exclusion_policy'] == full['exclusion_policy']
    assert 'https://github.com/harbor-framework/terminal-bench-2/issues/75' in (tmp_path / 'report.md').read_text()


@pytest.mark.parametrize('reward,passed,verified', [(1, 1, 1), (0, 0, 1), (None, 0, 0)])
def test_tool_failure_and_verification_are_independent(tmp_path, reward, passed, verified):
    runner = load_runner()
    rewards = {'reward': reward} if reward is not None else None
    assert runner.classify({'verifier_result': {'rewards': rewards}},
                           {'reason': 'unresolved_tool_error'}, False) == 'tool'
    rows = [dict(task='cleanup-failure', category='tool', native_status='failed',
                 native_reason='unresolved_tool_error', rewards=rewards)]
    ledger = Ledger(tmp_path / 'budget.json', total_limit=1, route_limits={})
    runner.report(tmp_path, rows, ledger, manifest={'selected_tasks': ['cleanup-failure']})
    summary = json.loads((tmp_path / 'summary.json').read_text())
    assert summary['verified_passes'] == passed
    assert summary['verified_tasks'] == verified
    assert summary['unverified_tasks'] == 1 - verified
    assert summary['categories'] == {'tool': 1}
    assert 'unresolved_tool_error' in (tmp_path / 'report.md').read_text()


def test_focused_rerun_selection_and_budget_are_explicit():
    runner = load_runner()
    tasks = fixture_tasks(runner)
    selected, plan = runner.select_tasks(tasks, 'reviewed-text', 'rerun', ['qemu-startup'])
    assert plan['selected_tasks'] == ['qemu-startup']
    assert runner.task_budget('rerun', 89) * len(selected) == .28
    for names in ([], ['qemu-startup', 'qemu-startup'], ['configure-git-webserver'], ['unknown']):
        with pytest.raises(SystemExit):
            runner.select_tasks(tasks, 'reviewed-text', 'rerun', names)
    with pytest.raises(SystemExit):
        runner.select_tasks(tasks, 'reviewed-text', 'full', ['qemu-startup'])
