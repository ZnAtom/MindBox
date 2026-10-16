"""Isolated action regressions; annotations are loaded only after inference ends."""
import argparse
import copy
import json
from pathlib import Path
import shutil

from .actions import VERSION, action_batch, reconcile, version_hash
from .client import write_json
from .coarse import CoarseIntake
from .config import ROOT
from .experiments import Experiment, file_hash


SOURCES = {
    'v7': ROOT / 'var/runs/state-actions-v8/v7',
    'v8': ROOT / 'var/runs/state-actions-v8/v8',
    'v9': ROOT / 'var/experiments/batch-a-v9-cold/outputs/v9',
    'v10': ROOT / 'var/experiments/batch-a-v10-cold/outputs/v10',
}


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def replay(source, destination, variant):
    shutil.copytree(source, destination)
    result = load(source / 'result.json')
    frames = {f['id']: f for f in load(source / 'action-media.json')['frames']}
    trace = load(source / 'action-tool-trace.json')
    frames.update({f['id']: f for t in trace['trace'] for f in t['result'].get('frames', [])})
    windows = copy.deepcopy(load(source / 'window-states.json'))
    if variant == 'original':
        for window in windows:
            window['observation'] = window.pop('initial_observation', window['observation'])
            window.pop('review_observation', None)
            window.pop('review_decision', None)
    analysis = reconcile(load(source / 'snapshot-states.json'), windows, frames)
    analysis.update(version=VERSION, scene_plan=load(source / 'scene-plan.json'),
                    rejected_state_claims=load(source / 'rejected-state-claims.json'),
                    replay={'source': str(source), 'variant': variant, 'model_calls': 0,
                            'source_states_sha256': file_hash(source / 'window-states.json')})
    result.update(operation_analysis=analysis, event_candidates=analysis['event_candidates'],
                  event_uncertainties=analysis['pending'], action_version_hash=version_hash(),
                  regression_mode='frozen_observation_replay_' + variant)
    write_json(destination / 'window-states.json', windows)
    write_json(destination / 'operation-analysis.json', analysis)
    write_json(destination / 'result.json', result)
    return result


def summarize(context, saved, result):
    gate = CoarseIntake(context)
    assessment = gate.assess(saved['run_id'])
    receipt = gate.commit(saved['run_id'])
    inventory = context.store.get_inventory_snapshot()
    analysis = result.get('operation_analysis', {})
    return {'recognition_status': result['status'], 'history_status': analysis.get('status'),
            'net_change_status': analysis.get('net_change_status'),
            'unobserved_prefixes': analysis.get('unobserved_prefixes', []),
            'events': [{k: e[k] for k in ('kind', 'candidate_ids', 'event_window_ms')}
                       for e in analysis.get('event_candidates', [])],
            'net_changes': analysis.get('net_changes', []), 'pending': analysis.get('pending', []),
            'placement_completions': analysis.get('placement_completions', []),
            'gate': assessment, 'receipt': receipt, 'inventory': inventory,
            'database_integrity': context.store.db.status()['integrity'],
            'output': str(context.root)}


def finalize_identities(root, names):
    from .database import utcnow
    from .identity import enrich_candidate

    report = load(root / 'report.json')
    for name in names:
        case = report['cases'][name]
        context = Experiment.open(root / name)
        if case['receipt']['status'] == 'committed':
            case['enrichment_receipt'] = enrich_candidate(context, case['gate']['run_id'],
                case['gate']['candidate_id'], case['receipt']['added_item_ids'][0])
        case['inventory'] = context.store.get_inventory_snapshot()
        case['operation_state'] = context.store.get_operation(case['gate']['operation_id'])['state']
        case['receipts'] = context.store.receipts('cabinet-01')
        case['finalized_at'] = utcnow()
        case['finalization'] = 'Same-item identity enrichment; no new model or ASR calls.'
        write_json(context.root / 'report.json', case)
        write_json(root / 'report.json', report)
        print(name, case['operation_state'], case['inventory']['inventory_revision'], flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True, help='New experiment parent; existing paths are rejected')
    parser.add_argument('--mode', choices=('replay', 'fresh', 'finalize'), default='replay')
    parser.add_argument('--variant', choices=('recorded', 'original'), default='recorded')
    parser.add_argument('--videos', nargs='+', choices=tuple(SOURCES), default=['v9', 'v10', 'v7', 'v8'])
    parser.add_argument('--model', default='deepseek-pro')
    parser.add_argument('--cache-from', type=Path, help='Seed exact-request caches from an explicit earlier regression')
    args = parser.parse_args()
    root = args.root.resolve()
    if args.mode == 'finalize':
        finalize_identities(root, args.videos)
        return
    if args.cache_from and args.mode != 'fresh':
        parser.error('Exact-request cache seeding is only available for fresh action runs')
    root.mkdir(parents=True, exist_ok=False)
    reports, results = {}, {}
    for name in args.videos:
        source, video = SOURCES[name], ROOT.parent.parent / f'data/{name}.mp4'
        context = Experiment.create(root, name, videos=[video], model=args.model)
        destination = context.root / 'outputs' / name
        if args.mode == 'fresh':
            if args.cache_from:
                source_cache = args.cache_from.resolve() / name / 'outputs/cache'
                hashes = {path.name: file_hash(path) for path in source_cache.glob('*.json')}
                if hashes:
                    shutil.copytree(source_cache, context.root / 'outputs/cache', dirs_exist_ok=True)
                    context.manifest['cache_seed'] = {'source': str(source_cache), 'hashes': hashes}
                    context.save_manifest()
            result = action_batch(video, source.parent, context.root / 'outputs',
                                  database=context.store.db.path, model=args.model)[0]
        else:
            result = replay(source, destination, args.variant)
        saved = context.store.import_run(destination)
        reports[name] = summarize(context, saved, result)
        reports[name].update(mode=args.mode, variant=args.variant if args.mode == 'replay' else None,
                             source=str(source), recognition_reused=True)
        results[name] = result
        write_json(context.root / 'report.json', reports[name])
        write_json(root / 'report.json', {'scope': 'Development regressions with frozen recognition; not blind accuracy.',
                  'mode': args.mode, 'variant': args.variant if args.mode == 'replay' else None, 'cases': reports})
        print(name, reports[name]['history_status'], reports[name]['receipt']['status'], flush=True)
    # This evaluation never supplies labels, expected actions, or ans.md to the model.
    from .evaluation import evaluate_case
    labels = load(ROOT.parent / 'datasets/action-regression.json')
    evaluated = {name: evaluate_case(value, labels['cases'][name]) for name, value in results.items()}
    write_json(root / 'evaluation.json', {'scope': 'Post-inference evaluation only.', 'cases': evaluated})


if __name__ == '__main__':
    main()
