"""Summarize recorded outcomes and usage without reading solutions or verifiers."""
import argparse
from collections import Counter
import json
from pathlib import Path


def summarize(data):
    phases = {}
    for name in ('diagnostic', 'pilot', 'pilot-runtime', 'full'):
        folder = data / name
        if not (folder / 'manifest.json').exists():
            continue
        manifest = json.loads((folder / 'manifest.json').read_text())
        budget = json.loads((folder / 'budget.json').read_text())
        rows = json.loads((folder / 'results.json').read_text()) if (folder / 'results.json').exists() else []
        categories = Counter(row['category'] for row in rows)
        requests = budget['requests']
        known = [r for r in requests if r['usage'] is not None]
        phases[name] = {
            'selected': len(manifest['selected_tasks']), 'attempted': len(rows),
            'finished': 'finished' in manifest,
            'passed': sum((r.get('rewards') or {}).get('reward') == 1 for r in rows),
            'categories': dict(categories), 'accounted_usd': budget['accounted_usd'],
            'known_usage_estimated_usd': sum(r['estimated_actual_usd'] for r in known),
            'requests': len(requests), 'requests_with_usage': len(known),
            'prompt_tokens': sum(r['usage']['prompt_tokens'] for r in known),
            'completion_tokens': sum(r['usage']['completion_tokens'] for r in known),
            'budget_denials': len(budget.get('denials', [])),
            'elapsed_seconds': manifest.get('finished', manifest['started']) - manifest['started'],
            'tasks': rows,
        }
    return {'phases': phases, 'total_accounted_usd': sum(p['accounted_usd'] for p in phases.values()),
            'total_known_usage_estimated_usd': sum(p['known_usage_estimated_usd'] for p in phases.values()),
            'user_reported_initial_credit_usd': 19.42, 'combined_estimated_ceiling_usd': 14.99}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    summary = summarize(args.data)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    lines = ['# Terminal-Bench 2.0 constrained baseline', '',
        'These results use one attempt per phase and shortened execution limits. '
        'They are not a leaderboard submission. Historical pilots are separate from the full run.', '',
        '| Phase | Coverage | Verified passes | Categories | Accounted USD |',
        '|---|---|---|---|---|']
    for name, phase in summary['phases'].items():
        lines.append(f'| {name} | {phase["attempted"]}/{phase["selected"]} | {phase["passed"]} | '
                     f'{phase["categories"]} | {phase["accounted_usd"]:.6f} |')
    lines.extend(['', f'Total accounted cost including unknown-usage reservations: '
                  f'${summary["total_accounted_usd"]:.6f}. Known reported usage at published rates: '
                  f'${summary["total_known_usage_estimated_usd"]:.6f}. These are estimates, not an invoice.', '',
                  'Full-run results are provisional until all selected tasks have outcomes. '
                  'A verifier reward of 1 is a pass; unverified attempts are unsuccessful, with their cause retained.', '',
                  'See the benchmark README for pinned versions, budgets, architecture, runtime, '
                  'streaming changes, and reproduction steps. Private raw artifacts and exact manifests '
                  'remain under `.skua/.tmp/m8/<phase>/`; `summary.json` contains outcome and usage totals.'])
    (args.output / 'report.md').write_text('\n'.join(lines) + '\n')
    print(json.dumps({k: v for k, v in summary.items() if k != 'phases'}))


if __name__ == '__main__':
    main()
