"""Bounded causal replay, open-world object descriptions and per-action transactions."""
import argparse
from collections import deque
import copy
import json
from pathlib import Path
import shutil

import cv2
import numpy as np
from PIL import Image

from .causal_schemas import CausalObservation
from .client import APIError, Client, image_part, write_json
from .config import Settings
from .database import DatabaseError, fingerprint
from .experiments import Experiment, file_hash
from .media import iter_action_frames, signature
from .pipeline import prompt, structured, validate_verification
from .policy import box_iou, descriptive_identity, guard_identity
from .quality import assess_quality
from .schemas import Verification, pixel_box

PROMPT = Path(__file__).parent / 'prompts/causal-v1.txt'
VERSION = 'causal-replay-v2'
IMAGE_BUDGET = 20
PROTOTYPE_BUDGET = 4
FRAME_BUDGET = IMAGE_BUDGET - PROTOTYPE_BUDGET


def validate_observation(value, frames, after_ms=None):
    ids = [track.track_id for track in value.tracks]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate track ID')
    boxes = []
    for track in value.tracks:
        last = -1
        for point in track.points:
            if point.frame_id not in frames:
                raise ValueError('Frame not delivered to this call')
            ms = frames[point.frame_id]['media_time_ms']
            if after_ms is not None and ms <= after_ms:
                raise ValueError('Overlap frames are context only; return points after the prior watermark')
            if ms <= last:
                raise ValueError('Track points must increase in media time')
            last = ms
            if point.region == 'frame_edge':
                x1, y1, x2, y2 = point.bbox
                if min(x1, y1, 1000 - x2, 1000 - y2) > 50:
                    raise ValueError('frame_edge box is not near a video boundary')
            if point.bbox:
                boxes.append((track.track_id, point.frame_id, point.bbox))
    for index, (tid, fid, box) in enumerate(boxes):
        if any(other != tid and frame == fid and box_iou(box, other_box) > 0.65
               for other, frame, other_box in boxes[index + 1:]):
            raise ValueError('Same-frame overlapping physical objects have competing track IDs')


def touches_video_edge(point):
    box = point.get('bbox')
    return bool(box and (min(box[:2]) <= 1 or max(box[2:]) >= 999))


class CausalLedger:
    def __init__(self, state=None):
        self.state = state if state is not None else {'tracks': {}, 'events': [], 'watermark_ms': -1}

    def apply(self, packet, frames, bootstrap=False):
        validate_observation(CausalObservation.model_validate(packet), frames)
        events, baselines = [], []
        for observation in packet['tracks']:
            tid = observation['track_id']
            entry = self.state['tracks'].setdefault(tid, {'presence': None, 'points': [],
                'item_id': None, 'pending': None, 'entry_edge': None, 'exit_edge': None,
                'outside_seen': None, 'rest_since': None, 'last_inside': None, 'blocked_reason': None})
            if entry.get('blocked_reason'):
                entry['pending'] = entry['blocked_reason']
                continue
            if (observation['association'] != 'supported' or observation['object_support'] == 'low'
                    or observation['target_kind'] != 'movable_item'):
                entry['pending'] = 'physical_instance_association_uncertain'
                continue
            if not entry.get('verified_identity'):
                entry['identity'] = descriptive_identity(observation)
            entry['observation'] = observation
            for point in observation['points']:
                ms = frames[point['frame_id']]['media_time_ms']
                old = next((p for p in entry['points'] if p['frame_id'] == point['frame_id']), None)
                if old:
                    if old['region'] != point['region'] or old['at_rest'] != point['at_rest']:
                        entry['pending'] = 'same_frame_track_state_conflict'
                        entry['blocked_reason'] = entry['pending']
                    continue
                if entry['points'] and ms < entry['points'][-1]['media_time_ms']:
                    entry['pending'] = 'late_reversed_track_evidence'
                    entry['blocked_reason'] = entry['pending']
                    continue
                point = {**point, 'media_time_ms': ms}
                entry['points'].append(point)
                if len(entry['points']) > 512:
                    entry['points'] = entry['points'][-512:]
                if entry.get('blocked_reason'):
                    continue
                region = point['region']
                if bootstrap:
                    if region == 'inside' and point['at_rest'] and entry['presence'] is None:
                        entry['presence'], entry['last_inside'] = True, point
                        baselines.append(tid)
                    elif region == 'frame_edge' or (region == 'outside' and touches_video_edge(point)):
                        entry['entry_edge'] = {**point, 'boundary_contact_source': 'visible_bbox_at_video_edge'}
                    continue
                phase = point.get('drawer_phase') or packet['drawer_phase']
                if packet['drawer_only'] or phase in ('closed', 'closing', 'unknown'):
                    continue
                if region in ('occluded', 'unknown'):
                    entry['rest_since'] = None
                    continue
                if region == 'inside':
                    entry['last_inside'] = point
                    entry['exit_edge'], entry['outside_seen'] = None, None
                    if entry['presence'] is True:
                        entry['pending'] = None
                        continue
                    if not point['at_rest']:
                        entry['rest_since'] = None
                        entry['pending'] = 'placement_not_released'
                        continue
                    entry['rest_since'] = entry['rest_since'] or point
                    if not entry['entry_edge']:
                        entry['pending'] = 'video_entry_boundary_not_observed'
                    elif ms - entry['rest_since']['media_time_ms'] >= 180:
                        event = self._event(tid, entry, 'return' if entry['item_id'] else 'add',
                                            entry['entry_edge'], point)
                        events.append(event)
                        entry.update(presence=True, pending=None, entry_edge=None, rest_since=None)
                elif region in ('outside', 'frame_edge'):
                    entry['rest_since'] = None
                    edge_contact = region == 'frame_edge' or touches_video_edge(point)
                    if entry['presence'] is True:
                        entry['outside_seen'] = entry['outside_seen'] or point
                        if edge_contact:
                            entry['exit_edge'] = {**point, 'boundary_contact_source': 'visible_bbox_at_video_edge'}
                        entry['pending'] = 'awaiting_video_boundary_exit'
                    elif edge_contact:
                        entry['entry_edge'] = {**point, 'boundary_contact_source': 'visible_bbox_at_video_edge'}
                        entry['pending'] = 'awaiting_placement'
                elif region == 'out_of_frame':
                    if entry['presence'] is True and entry['exit_edge'] and entry['last_inside']:
                        event = self._event(tid, entry, 'remove', entry['last_inside'], point)
                        events.append(event)
                        entry.update(presence=False, pending=None, exit_edge=None,
                                     outside_seen=None, rest_since=None, entry_edge=None)
                    elif entry['presence'] is True:
                        entry['pending'] = 'video_exit_boundary_not_observed'
                    else:
                        entry.update(entry_edge=None, rest_since=None, pending=None)
        return baselines, sorted(events, key=lambda e: (e['event_window_ms'][1], e['track_id']))

    @staticmethod
    def _event(tid, entry, kind, before, after):
        event = {'kind': kind, 'track_id': tid, 'candidate_ids': [tid],
            'before_frame_id': before['frame_id'], 'after_frame_id': after['frame_id'],
            'event_window_ms': [before['media_time_ms'], after['media_time_ms']],
            'boundary_evidence': copy.deepcopy(entry['points']), 'support': 'medium',
            'explanation': 'Observed video-boundary traversal and drawer-relative state sequence',
            'committed': False, 'source': VERSION}
        return event


def make_crop(track, entry, frames, root):
    points = [p for p in entry['points'] if p.get('bbox') and p['frame_id'] in frames]
    if not points:
        raise ValueError('No visible object crop')
    scored = []
    for point in points[-36:]:
        with Image.open(root / frames[point['frame_id']]['file']) as image:
            box = pixel_box(point['bbox'], *image.size, padding=0)
            crop = image.crop(box)
            sharpness, exposure, _ = signature(np.asarray(crop.convert('RGB')))
        # Prefer released, interior views over clipped motion views with sharp backgrounds.
        rank = (point['region'] == 'inside' and point['at_rest'],
                point['region'] != 'frame_edge', sharpness * (1 - exposure))
        scored.append((rank, point, crop, sharpness, exposure, box))
    _, point, crop, sharpness, exposure, box = max(scored, key=lambda x: x[0])
    ref = 'crop-' + fingerprint([track['track_id'], point['frame_id'], point['bbox']])[:20]
    path = 'crops/' + ref + '.jpg'
    (root / 'crops').mkdir(exist_ok=True)
    crop.save(root / path, quality=95)
    evidence = {ref: {'id': ref, 'candidate_id': track['track_id'], 'file': path,
        'frame_id': point['frame_id'], 'media_time_ms': point['media_time_ms'],
        'bbox_normalized_xyxy': point['bbox'], 'crop_pixel_xyxy': list(box),
        'crop_size': list(crop.size), 'crop_quality': {'sharpness': sharpness,
        'bad_exposure': exposure, 'image_edge_contacts': sum(v <= 1 or v >= 999 for v in point['bbox'])}}}
    candidate = {**entry['identity'], 'candidate_id': track['track_id'], 'target_kind': 'movable_item',
        'localization_status': 'usable', 'object_support': track['object_support'],
        'visibility_fraction': track['visibility_fraction'], 'occlusion_level': track['occlusion_level'],
        'evidence_refs': [ref], 'brand': None, 'variant': None}
    verified = entry.get('verified_identity', {}).get('verification', {})
    for field in ('target_kind', 'localization_status', 'object_support', 'visibility_fraction', 'occlusion_level'):
        if verified.get(field) is not None:
            candidate[field] = verified[field]
    candidate['visual_quality'] = assess_quality(candidate, evidence)
    return candidate, evidence


def persist_action(context, ledger, tid, event, frames, root, session_id, video, baseline=False):
    store, entry = context.store, ledger.state['tracks'][tid]
    identity = fingerprint([session_id, tid, 'baseline' if baseline else event['kind'],
                            None if baseline else event['event_window_ms']])[:32]
    operation_id = 'causal-' + identity
    with store.db.connect() as connection:
        prior = connection.execute('SELECT receipt_json FROM inventory_commits WHERE operation_id=?', (operation_id,)).fetchone()
    if prior:
        receipt = json.loads(prior[0])
        entry['item_id'] = (receipt['added_item_ids'] or receipt['updated_item_ids'] or receipt['removed_item_ids'])[0]
        return receipt
    candidate, evidence = make_crop(entry['observation'], entry, frames, root)
    candidate['location_id'] = 'cabinet-01:drawer-1'
    if candidate['visual_quality']['status'] == 'pending':
        return {'status': 'pending', 'reasons': candidate['visual_quality']['reasons']}
    directory = root / 'operations' / identity
    directory.mkdir(parents=True, exist_ok=True)
    snapshot = store.get_inventory_snapshot(include_removed=True)
    refs = {p['frame_id'] for p in entry['points']}
    if event:
        refs.update([event['before_frame_id'], event['after_frame_id']])
    selected = [frames[ref] for ref in refs if ref in frames]
    for data in selected + list(evidence.values()):
        destination = directory / data['file']
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / data['file'], destination)
    initial, final = (True, True) if baseline else (False, True) if event['kind'] in ('add', 'return') else (True, False)
    net = {'candidate_id': tid, 'change': 'kept' if baseline else 'add' if final else 'remove',
           'initial_present': initial, 'final_present': final}
    result = {'video': video.name, 'input_sha256': file_hash(video), 'status': 'completed',
        'inventory_context': snapshot, 'items': [candidate], 'evidence': evidence,
        'registration_kind': 'visible_initial_baseline' if baseline else 'filmed_action',
        'operation_analysis': {'status': 'consistent', 'pending': [], 'net_changes': [net],
                               'event_candidates': [] if baseline else [event]}}
    write_json(directory / 'result.json', result)
    write_json(directory / 'media.json', {'input_sha256': result['input_sha256'], 'frames': selected,
        'session_id': session_id, 'watermark_ms': ledger.state['watermark_ms']})
    saved = store.import_run(directory, operation_id=operation_id, capture_id=session_id,
                             base_revision=snapshot['inventory_revision'])
    location = 'cabinet-01:drawer-1'
    if baseline:
        receipt = store.register_visible_baseline(saved['run_id'], tid, location)
    else:
        decision = {'candidate_id': tid, 'action': event['kind'], 'evidence_refs': candidate['evidence_refs']}
        if event['kind'] != 'add':
            if not entry['item_id']:
                return {'status': 'pending', 'reasons': ['existing_item_identity_missing']}
            current = store.get_item(entry['item_id'])
            decision.update(item_id=current['id'], expected_version=current['version'])
        receipt = store.commit_inventory({**{k: saved[k] for k in ('operation_id', 'run_id', 'base_revision')},
            'idempotency_key': 'causal:' + identity, 'reviewed_by': 'policy:' + VERSION,
            'decisions': [decision]})
    entry['item_id'] = (receipt['added_item_ids'] or receipt['updated_item_ids'] or receipt['removed_item_ids'])[0]
    entry['latest_crop'] = {'file': next(iter(evidence.values()))['file'], 'media_time_ms': next(iter(evidence.values()))['media_time_ms']}
    return receipt


def verify_new_identity(client, ledger, tid, frames, root, stage):
    entry = ledger.state['tracks'][tid]
    if entry.get('verified_identity'):
        return
    candidate, evidence = make_crop(entry['observation'], entry, frames, root)
    ref, data = next(iter(evidence.items()))
    parts = [{'type': 'text', 'text': f'candidate_id={tid}; evidence_id={ref}; crop then original full frame'},
             image_part(root / data['file']), image_part(root / frames[data['frame_id']]['file'])]
    verified = structured(client, [{'role': 'system', 'content': prompt('verify')},
        {'role': 'user', 'content': parts}], stage, Verification,
        lambda value: validate_verification(value, evidence), max_tokens=1800).items[0].model_dump()
    if verified.get('object_support') is None:
        verified['object_support'] = entry['observation']['object_support']
    quality = assess_quality(verified, evidence)
    if quality['status'] == 'pending':
        raise ValueError('Independent crop verification failed: ' + ','.join(quality['reasons']))
    verified = guard_identity(verified)
    entry['identity'] = descriptive_identity(verified)
    entry['verified_identity'] = {'identity': entry['identity'], 'quality': quality,
                                 'verification': verified, 'evidence': evidence}
    entry['latest_crop'] = {'file': data['file'], 'media_time_ms': data['media_time_ms']}


class DeliveredFrames:
    """The read-only evidence boundary, independent of the decoder's source file."""
    def __init__(self, frames, watermark):
        self.frames, self.watermark, self.trace = frames, watermark, []

    def get_frames(self, start_ms, end_ms, limit=FRAME_BUDGET):
        if start_ms < 0 or end_ms > self.watermark or end_ms < start_ms or not 1 <= limit <= 32:
            result = {'status': 'rejected', 'reason': 'outside_delivered_frame_budget'}
        else:
            selected = sorted((f for f in self.frames.values() if start_ms <= f['media_time_ms'] <= end_ms),
                              key=lambda f: f['media_time_ms'])
            if len(selected) > limit:
                indices = np.linspace(0, len(selected) - 1, limit).round().astype(int)
                selected = [selected[index] for index in indices]
            result = {'status': 'ok', 'frames': selected, 'watermark_ms': self.watermark}
        self.trace.append({'origin': 'orchestrator', 'tool': 'get_frames',
                           'arguments': {'start_ms': start_ms, 'end_ms': end_ms, 'limit': limit}, 'result': result})
        return result


def observe(client, ledger, frames, root, bootstrap, step):
    if len(frames) > FRAME_BUDGET:
        raise ValueError('Too many full frames for provider image budget')
    allowed = {frame['id']: frame for frame in frames}
    public = [{'track_id': tid, 'item_id': entry['item_id'], 'presence': entry['presence'],
               'pending': entry['pending'], 'previous_points': entry['points'][-4:], **entry.get('identity', {})}
              for tid, entry in ledger.state['tracks'].items()]
    parts = [{'type': 'text', 'text': json.dumps({'bootstrap': bootstrap, 'ledger': public,
             'prior_watermark_ms': ledger.state['watermark_ms'],
             'watermark_ms': max(f['media_time_ms'] for f in frames)}, ensure_ascii=False)}]
    prototypes = [(tid, e['latest_crop']) for tid, e in ledger.state['tracks'].items() if e.get('latest_crop')]
    prototypes = prototypes[-PROTOTYPE_BUDGET:]
    for tid, crop in prototypes:
        parts += [{'type': 'text', 'text': 'Past identity prototype, not current presence: ' + tid},
                  image_part(root / crop['file'])]
    for frame in frames:
        usage = 'CONTEXT ONLY: no output points' if frame['media_time_ms'] <= ledger.state['watermark_ms'] else 'NEW: output points allowed'
        parts += [{'type': 'text', 'text': f'{usage}; full image; frame_id={frame["id"]}; media_time_ms={frame["media_time_ms"]}; normalized xyxy 0..1000 on this image, clip to visible image; never pixel coordinates'},
                  image_part(root / frame['file'])]
    write_json(root / f'prompt-images-{step:03d}.json', {'limit': IMAGE_BUDGET,
        'full_frames': [f['id'] for f in frames], 'prototype_tracks': [t for t, _ in prototypes],
        'image_count': len(frames) + len(prototypes)})
    return structured(client, [{'role': 'system', 'content': PROMPT.read_text(encoding='utf-8')},
        {'role': 'user', 'content': parts}], f'causal_{step}', CausalObservation,
        lambda value: validate_observation(value, allowed, ledger.state['watermark_ms']), max_tokens=6500).model_dump()


def commit_candidates(context, ledger, before_state, baselines, events, frames, root,
                      session_id, video, client=None, step=0, verifier=verify_new_identity):
    """Isolate perception failures; database failures stop at a recoverable checkpoint."""
    failed, receipts = {}, []
    for tid in dict.fromkeys(baselines + [e['track_id'] for e in events]):
        try:
            if client is not None:
                verifier(client, ledger, tid, frames, root, f'causal_verify_{step}_{tid}')
        except (ValueError, APIError, OSError) as exc:
            proposed = ledger.state['tracks'][tid]
            previous = copy.deepcopy(before_state['tracks'].get(tid))
            if previous is None:
                previous = {'presence': None, 'points': [], 'item_id': None,
                    'entry_edge': None, 'exit_edge': None, 'outside_seen': None,
                    'rest_since': None, 'last_inside': None, 'blocked_reason': None,
                    'identity': proposed.get('identity', {})}
            previous['pending'] = 'independent_object_verification_failed'
            previous['failed_observation'] = proposed.get('observation')
            ledger.state['tracks'][tid] = previous
            failed[tid] = client.safe(exc)
    accepted = [e for e in events if e['track_id'] not in failed]
    try:
        for tid in baselines:
            if tid in failed:
                continue
            receipt = persist_action(context, ledger, tid, None, frames, root, session_id, video, True)
            if receipt['status'] != 'committed':
                raise DatabaseError('Baseline not committed: ' + json.dumps(receipt))
            receipts.append(receipt)
        for event in accepted:
            receipt = persist_action(context, ledger, event['track_id'], event, frames, root, session_id, video)
            if receipt['status'] != 'committed':
                raise DatabaseError('Action not committed: ' + json.dumps(receipt))
            event['receipt'], event['committed'] = receipt, True
            ledger.state['events'].append(event)
            receipts.append(receipt)
    except (ValueError, APIError, OSError):
        # Replaying deterministic operations restores earlier successful commits.
        ledger.state = before_state
        raise
    return {'status': 'pending' if failed else 'observed', 'events': accepted,
            'receipts': receipts, 'verification_failures': failed}


def result_records(context, journal):
    records = []
    for step in journal['steps']:
        for receipt in step.get('receipts', []):
            ids = receipt['added_item_ids'] + receipt['updated_item_ids'] + receipt['removed_item_ids']
            event = next((e for e in step.get('events', []) if e['receipt']['commit_id'] == receipt['commit_id']), None)
            for item_id in ids:
                item = context.store.get_item(item_id)
                tid = event['track_id'] if event else next((tid for tid, t in journal['ledger']['tracks'].items()
                                                          if t['item_id'] == item_id), None)
                entry = journal['ledger']['tracks'].get(tid, {})
                records.append({'item_id': item_id, 'track_id': tid,
                    'kind': event['kind'] if event else 'initial_registration',
                    'filmed_inventory_action': event is not None,
                    'name': item['name'], 'name_kind': item.get('name_kind', 'unknown'),
                    'category': item['category'], 'description': item['description'],
                    'location_id': item['location_id'], 'stored_at': item.get('stored_at'),
                    'media_window_ms': event['event_window_ms'] if event else [0, 0],
                    'support': event['support'] if event else entry.get('observation', {}).get('object_support'),
                    'probability': None, 'evidence_refs': receipt['evidence_refs'],
                    'quality': entry.get('verified_identity', {}).get('quality'),
                    'operation_id': receipt['operation_id'], 'commit_id': receipt['commit_id'],
                    'revision': receipt['revision']})
    return records


def run(context, video, client, *, window_ms=750, fps=12, max_calls=32, observer=observe):
    if not 650 <= window_ms <= 5000 or not 1 <= fps <= 30 or not 1 <= max_calls <= 120:
        raise ValueError('Invalid causal replay budgets')
    video, root = Path(video).resolve(), context.root / 'causal'
    root.mkdir(exist_ok=True)
    source_hash = file_hash(video)
    if source_hash not in {v['sha256'] for v in context.manifest['videos']}:
        raise ValueError('Video outside the experiment input set')
    code_hashes = {name: file_hash(Path(__file__).with_name(name)) for name in
                   ('causal.py', 'causal_schemas.py', 'quality.py', 'media.py', 'policy.py', 'store.py')}
    config = fingerprint([VERSION, PROMPT.read_text(encoding='utf-8'), window_ms, fps, code_hashes,
                          getattr(getattr(client, 'settings', None), 'model', None)])
    journal_path = root / 'session.json'
    journal = json.loads(journal_path.read_text(encoding='utf-8')) if journal_path.exists() else {
        'session_id': 'session-' + fingerprint([context.manifest['scope_id'], source_hash])[:32],
        'input_sha256': source_hash, 'config_hash': config, 'steps': [],
        'ledger': {'tracks': {}, 'events': [], 'watermark_ms': -1}, 'status': 'running'}
    if journal['input_sha256'] != source_hash or journal['config_hash'] != config:
        raise ValueError('Resume configuration/input changed; use a new isolated experiment')
    if journal['status'] in ('completed', 'completed_with_pending'):
        return journal
    ledger, frames = CausalLedger(journal['ledger']), {}
    media_path = root / 'delivered-frames.json'
    if media_path.exists():
        frames = json.loads(media_path.read_text(encoding='utf-8'))
    cursor, last_call, previous, stable_since = ledger.state['watermark_ms'], ledger.state['watermark_ms'], None, None
    recent = deque(maxlen=160)

    def process_chunk(available, bootstrap=False):
        if len(journal['steps']) >= max_calls:
            raise ValueError('Causal micro-batch budget exhausted (verification/repair/retry calls counted separately)')
        watermark = available[-1]['media_time_ms']
        step = len(journal['steps'])
        evidence_tool = DeliveredFrames({f['id']: f for f in available}, watermark)
        supplied = evidence_tool.get_frames(available[0]['media_time_ms'], watermark)['frames']
        write_json(root / 'delivered-frames.json', frames)
        write_json(root / f'tools-{step:03d}.json', evidence_tool.trace)
        path = root / f'observation-{step:03d}.json'
        before_state = copy.deepcopy(ledger.state)
        committing = False
        try:
            packet = json.loads(path.read_text(encoding='utf-8')) if path.exists() else observer(client, ledger, supplied, root, bootstrap, step)
            validate_observation(CausalObservation.model_validate(packet), {f['id']: f for f in supplied},
                                 ledger.state['watermark_ms'])
            write_json(path, packet)
            baselines, events = ledger.apply(packet, {f['id']: f for f in supplied}, bootstrap)
            ledger.state['watermark_ms'] = watermark
            committing = True
            outcome = commit_candidates(context, ledger, before_state, baselines, events, frames,
                root, journal['session_id'], video, client, step)
        except (ValueError, APIError, OSError) as exc:
            # A model/validation failure must not alter the prior confirmed ledger.
            ledger.state = before_state
            if committing:
                # Stop at this checkpoint. Deterministic operation IDs recover any DB commit
                # that completed before a process/file failure, on the next explicit resume.
                raise
            ledger.state['watermark_ms'] = watermark
            outcome = {'status': 'pending', 'error': client.safe(exc) if client else str(exc)[:800]}
        journal['steps'].append({'index': step, 'bootstrap': bootstrap, 'watermark_ms': watermark,
            'supplied_frame_ids': [f['id'] for f in supplied], **outcome})
        journal['ledger'] = ledger.state
        write_json(journal_path, journal)

    try:
        for frame in iter_action_frames(video, root, fps=fps):
            frames[frame['id']] = frame
            recent.append(frame)
            if frame['media_time_ms'] <= cursor:
                continue
            if cursor < 0:
                process_chunk([frame], True)
                cursor = last_call = frame['media_time_ms']
                continue
            with Image.open(root / frame['file']) as image:
                gray = cv2.resize(cv2.cvtColor(np.asarray(image.convert('RGB')), cv2.COLOR_RGB2GRAY), (256, 144))
            motion = float(np.mean(cv2.absdiff(gray, previous))) if previous is not None else 255
            previous = gray
            if motion < 2.5:
                stable_since = stable_since if stable_since is not None else frame['media_time_ms']
            else:
                stable_since = None
            age = frame['media_time_ms'] - last_call
            if age >= window_ms or (age >= 650 and stable_since is not None and frame['media_time_ms'] - stable_since >= 250):
                available = [f for f in recent if last_call - 750 <= f['media_time_ms'] <= frame['media_time_ms']]
                process_chunk(available)
                last_call = frame['media_time_ms']
        if recent and recent[-1]['media_time_ms'] > last_call:
            process_chunk([f for f in recent if f['media_time_ms'] >= last_call - 750])
        journal['status'] = 'completed_with_pending' if (
            any(s['status'] == 'pending' for s in journal['steps']) or
            any(t['pending'] for t in ledger.state['tracks'].values())) else 'completed'
    except (ValueError, OSError, APIError) as exc:
        journal.update(status='paused_error', error=client.safe(exc) if client else str(exc)[:800])
    journal['ledger'] = ledger.state
    journal['inventory'] = context.store.get_inventory_snapshot(include_removed=True)
    journal['database_integrity'] = context.store.db.status()['integrity']
    journal['results'] = result_records(context, journal)
    journal['decode_complete'] = journal['status'] in ('completed', 'completed_with_pending')
    journal['budget'] = {'max_micro_batches': max_calls, 'max_images_per_observation': IMAGE_BUDGET,
                         'note': 'Independent verification, one repair, and HTTP retries add requests.'}
    journal['scope'] = 'Causal file replay; no live K230 transport or WebUI. Frame regions/associations are model estimates.'
    write_json(root / 'delivered-frames.json', frames)
    write_json(journal_path, journal)
    return journal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--model', default='deepseek-pro')
    parser.add_argument('--window-ms', type=int, default=750)
    parser.add_argument('--fps', type=int, default=12)
    parser.add_argument('--max-calls', type=int, default=32)
    args = parser.parse_args()
    settings = Settings.load()
    settings.model = args.model
    context = Experiment.open(args.root) if args.resume else Experiment.create(
        args.root.parent, args.root.name, videos=[args.video], model=args.model)
    report = run(context, args.video, Client(settings, context.root / 'api'),
                 window_ms=args.window_ms, fps=args.fps, max_calls=args.max_calls)
    print(json.dumps({'status': report['status'], 'steps': len(report['steps']),
        'events': [(event['kind'], event['track_id'], event['committed']) for event in report['ledger']['events']],
        'pending': {tid: entry['pending'] for tid, entry in report['ledger']['tracks'].items() if entry['pending']},
        'revision': report['inventory']['inventory_revision']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
