"""Report categories must preserve unsuccessful infrastructure/task distinctions."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('benchmark_run', Path(__file__).parents[1] / 'benchmark/run.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_failure_categories_and_limits():
    classify = module.classify
    assert classify({'verifier_result': {'rewards': {'reward': 1}}}, {}, False) == 'passed'
    assert classify({'verifier_result': {'rewards': {'reward': 0}}}, {}, False) == 'task'
    assert classify({'exception_info': {'exception_type': 'AgentSetupTimeoutError'}}, {}, False) == 'infrastructure'
    assert classify({}, {'reason': 'time_limit'}, False) == 'timeout'
    assert classify({}, {'reason': 'turn_error'}, False) == 'model'
    assert classify({}, {'reason': 'unresolved_tool_error'}, False) == 'tool'
    assert classify({}, {'status': 'approval_required'}, False) == 'policy'
    assert classify({}, {}, False) == 'infrastructure'
    assert classify({}, {}, True) == 'cost_limit'
    assert sum(module.LIMITS.values()) <= 15


def test_summary_keeps_unknown_usage_reservations_separate(tmp_path):
    import json
    spec = importlib.util.spec_from_file_location('benchmark_summary', Path(__file__).parents[1] / 'benchmark/summarize.py')
    summary_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(summary_module)
    folder = tmp_path / 'full'
    folder.mkdir()
    (folder / 'manifest.json').write_text(json.dumps({'selected_tasks': ['a', 'b'], 'started': 10, 'finished': 20}))
    (folder / 'results.json').write_text(json.dumps([{'category': 'tool', 'rewards': {'reward': 1}}, {'category': 'infrastructure'}]))
    (folder / 'budget.json').write_text(json.dumps({'accounted_usd': .11, 'requests': [
        {'usage': {'prompt_tokens': 20, 'completion_tokens': 5}, 'estimated_actual_usd': .01},
        {'usage': None, 'estimated_actual_usd': None}]}))
    result = summary_module.summarize(tmp_path)
    assert result['total_accounted_usd'] == .11
    assert result['total_known_usage_estimated_usd'] == .01
    assert result['phases']['full']['requests_with_usage'] == 1
    assert result['phases']['full']['passed'] == 1
    assert result['phases']['full']['attempted'] == 2


import json
import pytest


@pytest.mark.parametrize('raw,flag', [
    (None, 'native_result_missing'), ('', 'native_result_empty'),
    ('  \n', 'native_result_empty'), ('{broken', 'native_result_invalid'),
    ('[]', 'native_result_invalid'), ('null', 'native_result_invalid'),
    ('{"status": [], "reason": {}}', 'native_result_invalid'),
])
def test_bad_native_result_preserves_timeout_reward_and_path(tmp_path, raw, flag):
    trial = tmp_path / 'jobs/mailman/trial'
    (trial / 'agent').mkdir(parents=True)
    (trial / 'result.json').write_text(json.dumps({
        'exception_info': {'exception_type': 'AgentTimeoutError'},
        'verifier_result': {'rewards': {'reward': 0}}}))
    if raw is not None:
        (trial / 'agent/result.json').write_text(raw)
    (trial / 'agent/session.jsonl').write_text('')
    row = module.collect_outcome(tmp_path, 'mailman', 0, [], [])
    assert row['category'] == 'timeout'
    assert row['exception_type'] == 'AgentTimeoutError'
    assert row['rewards'] == {'reward': 0}
    assert row['trial'] == 'jobs/mailman/trial'
    assert flag in row['secondary_flags']
    assert 'transcript_empty' in row['secondary_flags']
    assert 'harness_exception_type' not in row


def test_bad_transcript_keeps_valid_events_and_gateway_signals(tmp_path):
    trial = tmp_path / 'jobs/task/trial'
    (trial / 'agent').mkdir(parents=True)
    (trial / 'result.json').write_text('{"verifier_result":{"rewards":{"reward":1}}}')
    (trial / 'agent/result.json').write_text('{"status":"failed","reason":"unresolved_tool_error"}')
    (trial / 'agent/session.jsonl').write_text('\n'.join([
        '{"type":"tool_result","status":"error","error":{"code":"invalid_arguments"}}',
        '[]', '{broken', '{"type":"tool_result","error":5}']))
    row = module.collect_outcome(tmp_path, 'task', 0,
        [{'route': '/v1/task', 'finish_reason': 'length'}], [])
    assert row['category'] == 'tool' and row['rewards'] == {'reward': 1}
    assert row['secondary_flags'] == ['invalid_arguments', 'response_truncated', 'transcript_unreadable']


@pytest.mark.parametrize('raw', ['{bad', '[]', '{"exception_info":4,"verifier_result":[]}'])
def test_bad_harbor_result_retains_trial_path(tmp_path, raw):
    trial = tmp_path / 'jobs/task/trial'
    trial.mkdir(parents=True)
    (trial / 'result.json').write_text(raw)
    row = module.collect_outcome(tmp_path, 'task', 1, [], [])
    assert row['category'] == 'infrastructure'
    assert row['trial'] == 'jobs/task/trial'
    assert 'trial_result_invalid' in row['secondary_flags']


def test_ambiguous_trials_are_not_arbitrarily_combined(tmp_path):
    for name in ('one', 'two'):
        trial = tmp_path / 'jobs/task' / name
        trial.mkdir(parents=True)
        (trial / 'result.json').write_text('{"verifier_result":{"rewards":{"reward":1}}}')
    row = module.collect_outcome(tmp_path, 'task', 0, [], [])
    assert row['category'] == 'infrastructure' and row['rewards'] is None
    assert row['trial'] is None and row['secondary_flags'] == ['trial_result_ambiguous']


def test_git_workaround_is_scoped_to_reviewed_image_digests(tmp_path):
    for image in module.GIT_BOOTSTRAP_IMAGES:
        assert module.git_bootstrap_profile(image) == 'bullseye-20251020'
        assert module.git_bootstrap_profile(image.split('@')[0] + ':latest') == 'default'
    assert module.git_bootstrap_profile('unrelated@sha256:abc') == 'default'
    configured = module.agent_config(tmp_path, 'mock', 'http://mock.invalid', 300,
                                      'bullseye-20251020')
    assert configured['kwargs']['git_bootstrap'] == 'bullseye-20251020'
    assert module.agent_config(tmp_path, 'mock', 'http://mock.invalid', 300)['kwargs']['git_bootstrap'] == 'default'
