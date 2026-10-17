"""Offline forensic replays and inventory/speech/task integration on isolated copies."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import sqlite3

from app.actions import reconcile
from app.client import write_json
from app.config import ROOT
from app.evaluation import evaluate_case
from app.experiments import Experiment, file_hash
from app.speech import SpeechService
from app.tasks import Tasks


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def frames_from(root):
    frames = {f['id']: f for f in load(root / 'action-media.json')['frames']}
    for call in load(root / 'action-tool-trace.json')['trace']:
        frames.update({f['id']: f for f in call['result'].get('frames', [])})
    matches = root / 'appearance-matches.json'
    if matches.exists():
        for frame_id, hints in load(matches).items():
            if frame_id in frames:
                frames[frame_id]['appearance_hints'] = hints
    return frames


def restore_object(windows, old_windows, window_id, candidate_id):
    window = next(w for w in windows if w['window_id'] == window_id)
    old = next(w for w in old_windows if w['window_id'] == window_id)
    replacement = next(o for o in old['observation']['objects'] if o['candidate_id'] == candidate_id)
    for key in ('observation', 'initial_observation'):
        if key in window:
            window[key]['objects'] = [copy.deepcopy(replacement) if o['candidate_id'] == candidate_id else o
                                      for o in window[key]['objects']]
    if 'review_observation' in window:
        window['review_observation']['objects'] = [o for o in window['review_observation']['objects']
                                                   if o['candidate_id'] != candidate_id]
        if not window['review_observation']['objects']:
            window.pop('review_observation')
            window.pop('review_decision', None)


def action_review(old, new, output):
    old, new = old.resolve(), new.resolve()
    old_snapshots, new_snapshots = (load(p / 'snapshot-states.json') for p in (old, new))
    old_windows, new_windows = (load(p / 'window-states.json') for p in (old, new))
    old_frames, new_frames = frames_from(old), frames_from(new)
    frames = {**old_frames, **new_frames}
    result = load(new / 'result.json')
    variants = {
        'old_observations_current_reconciler': (old_snapshots, old_windows),
        'new_observations_current_reconciler': (new_snapshots, new_windows),
        'new_snapshots_old_windows': (new_snapshots, old_windows),
        'old_snapshots_new_windows': (old_snapshots, new_windows),
    }
    for name, changes in (
        ('restore_candy_c2_only', [('c2', 'obj03')]),
        ('restore_chicken_c4_only', [('c4', 'obj01')]),
        ('restore_three_window_claims_and_snapshots', [('c1', 'obj03'), ('c2', 'obj03'), ('c4', 'obj01')]),
    ):
        windows = copy.deepcopy(new_windows)
        for window_id, candidate_id in changes:
            restore_object(windows, old_windows, window_id, candidate_id)
        variants[name] = (old_snapshots if len(changes) == 3 else new_snapshots, windows)
    outputs = {}
    for name, (snapshots, windows) in variants.items():
        analysis = reconcile(snapshots, windows, frames)
        outputs[name] = {'analysis': analysis}
    # Labels are used only to score completed counterfactual analyses, never as observations.
    labels = load(ROOT.parent / 'datasets/action-regression.json')['cases']['v7']
    for value in outputs.values():
        analysis = value['analysis']
        value['evaluation'] = evaluate_case({**result, 'operation_analysis': analysis,
                                              'event_candidates': analysis['event_candidates']}, labels)
    evidence = load(old / 'evidence.json')
    evidence_hashes = {key: {'old': file_hash(old / value['file']), 'new': file_hash(new / value['file'])}
                       for key, value in evidence.items()}
    unchanged = {name: file_hash(old / name) == file_hash(new / name)
                 for name in ('action-media.json', 'evidence.json', 'appearance-matches.json', 'coverage-windows.json')}
    old_media, new_media = (load(p / 'action-media.json') for p in (old, new))
    common_frames = old_frames.keys() & new_frames.keys()
    report = {'scope': 'Evaluation-only counterfactual substitutions of saved observations; no new recognition, model calls or DB writes.',
              'sources': {'old': str(old), 'new': str(new)}, 'unchanged_artifacts': unchanged,
              'action_media_changed_fields': [k for k in old_media if old_media[k] != new_media.get(k)],
              'common_decoded_frames': len(common_frames),
              'common_frame_times_equal': all(old_frames[k]['media_time_ms'] == new_frames[k]['media_time_ms']
                                              for k in common_frames),
              'common_frame_bytes_equal': all(file_hash(old / old_frames[k]['file']) == file_hash(new / new_frames[k]['file'])
                                              for k in common_frames),
              'crop_hashes': evidence_hashes,
              'all_crop_bytes_equal': all(v['old'] == v['new'] for v in evidence_hashes.values()),
              'frames': {key: {field: frame[field] for field in ('media_time_ms', 'file')}
                         for key, frame in frames.items()},
              'variants': outputs,
              'historical_saved_evaluation': evaluate_case(load(old / 'result.json'), labels),
              'limits': ['Substitutions diagnose causes; they do not fix the visual observer or prove future accuracy.',
                         'Old success means event order and net change passed, not fine identity or inventory execution.',
                         'Scene ROI, prompts and sequence input changed together; their separate model effects remain unmeasured.']}
    write_json(output, report)
    return {name: {'pass': value['evaluation']['pass'], 'history': value['evaluation']['history'],
                   'net': value['evaluation']['net'], 'pending': len(value['analysis']['pending'])}
            for name, value in outputs.items()}


def copy_experiment(source, destination):
    destination.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect((source / 'inventory.sqlite3').resolve().as_uri() + '?mode=ro', uri=True) as source_db:
        with sqlite3.connect(destination / 'inventory.sqlite3') as target_db:
            source_db.backup(target_db)
    shutil.copy2(source / 'manifest.json', destination / 'manifest.json')
    shutil.copytree(source / 'inventory-assets', destination / 'inventory-assets')
    return Experiment.open(destination)


def speech_review(source, transcripts, root, removal_policy_reason=None):
    source, transcripts, root = source.resolve(), transcripts.resolve(), root.resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {'scope': 'Latest committed inventory plus frozen real ASR/slots on independent SQLite copies; no model/ASR rerun or delivery.',
              'cases': {}, 'model_requests': 0, 'asr_runs': 0}
    for name in ('v9', 'v10'):
        original = source / name
        speech_source = ROOT / 'var/experiments' / f'batch-a-{name}-cold'
        protected = [original / 'inventory.sqlite3', original / 'manifest.json']
        before_hashes = {str(path): file_hash(path) for path in protected}
        context = copy_experiment(original, root / name)
        before = context.store.get_inventory_snapshot()
        asr_path, slots_path = transcripts / f'{name}-final.json', speech_source / 'asr/slots.json'
        asr, slots_record = load(asr_path), load(slots_path)
        if slots_record['source_text'] != asr['text']:
            raise ValueError('Frozen slots must quote the exact saved ASR text')
        audio = speech_source / 'outputs' / name / 'audio.wav'
        media = load(speech_source / 'outputs' / name / 'media.json')
        video_hash = load(original / 'manifest.json')['videos'][0]['sha256']
        service, tasks = SpeechService(context), Tasks(context)
        policy = removal_policy_reason if name == 'v10' else None
        passes = []
        for _ in range(2):
            utterance = service.ingest(video_hash, audio, asr, slots_record['slots'], media['audio']['first_media_time_ms'])
            linked = service.link(utterance['id'])
            facts = service.apply_facts(utterance['id'])
            tasks.draft(utterance['id'], policy)
            passes.append({'utterance_id': utterance['id'], 'item_id': linked['item_id'],
                           'facts_receipt': facts, 'inventory_revision': context.store.get_inventory_snapshot()['inventory_revision'],
                           'task_ids': sorted(t['id'] for t in tasks.list())})
        linked = service.get(utterance['id'])
        with context.store.db.connect() as connection:
            counts = {table: connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                      for table in ('items', 'inventory_commits', 'speech_utterances', 'task_commands', 'reminders', 'deliveries', 'jobs')}
        case = {'source_inventory': str(original), 'database': str(context.store.db.path),
                'frozen_evidence': {str(p): file_hash(p) for p in (asr_path, slots_path, audio)},
                'transcript': asr['text'], 'slots': slots_record['slots'],
                'speech_link_status': linked['link_status'], 'speech_item_id': linked['item_id'],
                'link_evidence': linked['parsed']['link'], 'date_candidates': linked['parsed']['facts'],
                'facts_receipt': facts, 'tasks': tasks.list(), 'counts': counts,
                'inventory_before': before, 'inventory_after': context.store.get_inventory_snapshot(),
                'repeat_is_idempotent': passes[0] == passes[1],
                'source_unchanged': before_hashes == {str(path): file_hash(path) for path in protected},
                'database_integrity': context.store.db.status()['integrity'],
                'delivery_state': 'not_scheduled_missing_inputs', 'configured_removal_policy': policy}
        report['cases'][name] = case
        write_json(context.root / 'review.json', case)
    write_json(root / 'report.json', report)
    return {name: {k: case[k] for k in ('speech_link_status', 'speech_item_id', 'counts', 'repeat_is_idempotent', 'source_unchanged')}
            for name, case in report['cases'].items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    action = sub.add_parser('v7')
    action.add_argument('--old', type=Path, default=ROOT / 'var/runs/state-actions-v8/v7')
    action.add_argument('--new', type=Path, default=ROOT / 'var/experiments/p0-final-20261017-01/v7/outputs/v7')
    action.add_argument('--output', type=Path, default=ROOT / 'var/review/v7-module-review.json')
    speech = sub.add_parser('speech')
    speech.add_argument('--source', type=Path, default=ROOT / 'var/experiments/p0-final-20261017-01')
    speech.add_argument('--transcripts', type=Path, default=ROOT / 'var/runs/batch-a-asr')
    speech.add_argument('--root', type=Path, required=True)
    speech.add_argument('--removal-policy-reason')
    args = parser.parse_args()
    result = (action_review(args.old, args.new, args.output) if args.command == 'v7'
              else speech_review(args.source, args.transcripts, args.root, args.removal_policy_reason))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
