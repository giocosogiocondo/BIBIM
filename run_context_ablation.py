"""Run matched A/B/C contexts on locally prepared questions; no paid judging."""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from experiments.text_to_sql_top3 import runner
from experiments.text_to_sql_top3.schema import load_config, load_prepared_sql
from experiments.view_selection_ablation.common import file_hash, write_json
from experiments.view_selection_ablation.evaluation import rank_options

ROOT = Path(__file__).resolve().parent


class RoutingMismatch(Exception):
    pass


class ControlledRouter:
    def __init__(self, router, expected):
        self.router, self.expected = router, expected

    def score(self, *args, **kwargs):
        score = self.router.score(*args, **kwargs)
        if rank_options(score['probabilities'])[:3] != self.expected:
            raise RoutingMismatch('Top3 differs from the first condition; no SQL generated')
        return score


def latest_matching(folder, ids):
    for path in reversed(sorted(Path(folder).glob('*/run_manifest.json'))):
        if json.loads(path.read_text())['question_ids'] == ids:
            return path.parent
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'configs/pipeline.local.json')
    parser.add_argument('--conditions', default='ABC')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--fresh', action='store_true')
    args = parser.parse_args()
    if not args.conditions or len(set(args.conditions)) != len(args.conditions) or set(args.conditions)-set('ABC'):
        parser.error('Choose unique conditions from A, B and C')
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    base = json.loads(args.config.read_text(encoding='utf-8'))
    baseline, baseline_source = None, None
    configs = ROOT/'.cache/ablation_configs'
    configs.mkdir(parents=True, exist_ok=True)
    for label in args.conditions:
        cfg = {**base, 'column_context': 'names_only' if label=='A' else 'statistics',
               'column_descriptions': label=='C', 'ablation_condition': label,
               'results_directory': str(Path(base['results_directory'])/('ablation_'+label)),
               'ablation_launcher_sha256': file_hash(Path(__file__)),
               'ablation_baseline_sha256': file_hash(baseline_source) if baseline_source else None}
        config_path = configs/(label+'.json')
        write_json(config_path, cfg)
        config = load_config(config_path)
        if not config.get('single_pass'):
            raise ValueError('Context ablation requires the single-pass configuration')
        _, _, cases, _ = load_prepared_sql(config)
        ids = [c['id'] for c in runner.choose_cases(cases, config, limit=args.limit)]
        resume = None if args.fresh else latest_matching(config['results_directory'], ids)
        original = runner.process_case

        def controlled(case, config, inventory, schemas, generator, router, reference, **kwargs):
            if baseline is not None:
                router = ControlledRouter(router, baseline[case['id']])
            record = original(case, config, inventory, schemas, generator, router, reference, **kwargs)
            record['ablation_condition'] = label
            record['top3_control'] = 'baseline' if baseline is None else 'checked before SQL'
            return record

        print('Condition '+label, flush=True)
        with patch.object(runner, 'process_case', controlled):
            summary = runner.run(config, ids=ids, resume=resume)
        if not summary['complete']:
            return 1
        if baseline is None:
            folder = latest_matching(config['results_directory'], ids)
            baseline_source = folder/'records.jsonl'
            records = [json.loads(s) for s in baseline_source.read_text(encoding='utf-8').splitlines()]
            if any('routing' not in r for r in records):
                raise ValueError('First condition has a routing failure; cannot establish matched Top3')
            baseline = {r['id']: r['routing']['ranking'][:3] for r in records}
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
