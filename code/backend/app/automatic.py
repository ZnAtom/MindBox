"""Production adapter for the evaluated sliced analyzer and controlled Store writes."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil

from PIL import Image

from .client import image_part, write_json
from .config import Settings
from .database import Conflict, encode, fingerprint
from .policy import compact, descriptive_identity
from model_comparison.long_video import (Packet, apply_packet, digest, plan_slices,
    prototypes, read_slice)
from model_comparison.long_video_audit import unresolved
from model_comparison.responses_client import ResponsesClient

VERSION = 'automatic-v1'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def inside_path(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise ValueError('Missing or invalid session evidence')
    return path


def auto_settings(*, api_key=None):
    Settings.load()
    key = api_key or os.getenv('MIND_BOX_AUTO_API_KEY', '')
    if not key:
        raise ValueError('MIND_BOX_AUTO_API_KEY is required')
    return Settings(os.getenv('MIND_BOX_AUTO_API_URL', 'https://api.opentech.top/v1/responses'),
                    key, os.getenv('MIND_BOX_AUTO_MODEL', 'gpt-6.1-sol'), attempts=2, timeout=240)


class CaptureClient(ResponsesClient):
    def __init__(self, settings, root, manifest):
        super().__init__(settings, root / 'api')
        self.root = root
        self.seeds = read_json(root / 'inventory-context.json')['seeds']
        self.context = ('这是实际柜子的连续自动记录。ledger 中 inventory_item_id 是已有库存身份；'
            '历史外观图只用于辨认。能确定是原件才沿用 ID，不能只凭同名/同款合并；'
            '不确定时 identity_support=low 并提 uncertain，禁止另建一个重复物品。'
            'removed 状态的物品也可能被放回。新物品必须使用未占用的 obj-NNN。'
            '抽屉编号最终由本机视觉识别码决定，下方是其主机到达时间轴，'
            'received_ms 减 origin_ms 为图片 TIME_MS。不要依据屏幕上下位置猜柜层。'
            + encode({'origin_ms': manifest['origin_ms'], 'doors': manifest['doors']}))

    def chat(self, messages, *args, **kwargs):
        labels = {part.get('text', '') for message in messages if isinstance(message.get('content'), list)
                  for part in message['content']}
        references = []
        for oid, seed in self.seeds.items():
            if seed.get('prototype') and 'HISTORICAL IDENTITY REFERENCE ONLY: ' + oid not in labels:
                references += [{'type': 'text', 'text': 'HISTORICAL IDENTITY REFERENCE ONLY: ' + oid},
                               image_part(self.root / seed['prototype']['file'])]
        extra = [{'role': 'user', 'content': references}] if references else []
        return super().chat([{'role': 'system', 'content': self.context}] + extra + messages, *args, **kwargs)


def location_at(manifest, ms):
    absolute = ms + manifest['origin_ms']
    states = [s for s in manifest['doors'] if s['received_ms'] <= absolute]
    if not states:
        return None
    state = states[-1]
    drawers = [i + 1 for i, value in enumerate(state['open']) if value]
    if not all(state['known']) or len(drawers) != 1:
        return None
    return manifest['cabinet_id'] + ':drawer-' + str(drawers[0])


class AutomaticRecorder:
    def __init__(self, sessions, *, client_factory=CaptureClient, slice_reader=read_slice,
                 settings_factory=auto_settings):
        self.sessions, self.store = sessions, sessions.store
        self.client_factory, self.slice_reader = client_factory, slice_reader
        self.settings_factory = settings_factory

    def context(self, manifest, root):
        path = root / 'inventory-context.json'
        if path.exists():
            return read_json(path)
        snapshot = self.store.get_inventory_snapshot(manifest['cabinet_id'], include_removed=True)
        # Bound references without silently discarding possible physical identities.
        if len(snapshot['items']) > 24:
            raise Conflict('identity_reference_capacity_exceeded')
        seeds = {}
        for index, item in enumerate(snapshot['items'], 1):
            oid = 'obj-%03d' % index
            seed = {'object_id': oid, 'inventory_item_id': item['id'],
                **{k: item.get(k) for k in ('name', 'category', 'description')},
                'readable_text': [], 'identity_support': 'medium', 'metadata_history': [],
                'presence': 'outside' if item['presence_status'] == 'removed' else 'inside',
                'ever_inside': True, 'location': item['location_id'], 'points': []}
            observations = self.store.get_item(item['id'])['observations']
            crops = [o for o in observations if o['kind'] == 'crop']
            if crops:
                source = self.store.db.path.parent / crops[-1]['asset_path']
                if hashlib.sha256(source.read_bytes()).hexdigest() != crops[-1]['sha256']:
                    raise ValueError('Inventory identity reference hash mismatch')
                destination = 'identity/' + oid + '.jpg'
                (root / 'identity').mkdir(exist_ok=True)
                shutil.copyfile(source, root / destination)
                seed['prototype'] = {'file': destination}
            seeds[oid] = seed
        value = {'snapshot': snapshot, 'seeds': seeds, 'version': VERSION}
        write_json(path, value)
        return value

    def analyze(self, manifest, root, context):
        path = root / 'analysis.json'
        if path.exists():
            value = read_json(path)
            if value['manifest_sha256'] != digest(manifest):
                raise Conflict('Session changed after analysis')
            return value['state']
        settings = self.settings_factory()
        client = self.client_factory(settings, root, manifest)
        state = {'model': settings.model, 'objects': copy.deepcopy(context['seeds']), 'events': [],
                 'proposals': [], 'baselines': [], 'conflicts': [], 'summaries': []}
        plan = {'frames': manifest['frames'], 'slices': plan_slices(manifest['frames'])}
        lookup = {f['id']: f for f in manifest['frames']}
        if len(plan['slices']) > 32:
            raise ValueError('Session exceeds the analysis slice budget')
        for part in plan['slices']:
            packet_path = root / 'slices' / part['id'] / 'packet.json'
            before_hash = digest(state)
            if packet_path.exists():
                record = read_json(packet_path)
                if (record['before_sha256'] != before_hash or record['model'] != settings.model
                        or record['version'] != VERSION):
                    raise Conflict('Cached analysis configuration changed')
                packet = Packet.model_validate(record['packet']).model_dump()
            else:
                packet, trace = self.slice_reader(client, plan, part, state, root)
                packet = Packet.model_validate(packet).model_dump()
                write_json(packet_path, {'before_sha256': before_hash, 'model': settings.model,
                    'version': VERSION, 'packet': packet, 'tools': trace})
            apply_packet(state, packet, lookup, part)
            prototypes(state, lookup, root)
            self.sessions.update(manifest['id'], 'processing', {'slice': part['id'],
                'slices': len(plan['slices']), 'objects': len(state['objects']), 'events': len(state['events'])})
        write_json(path, {'manifest_sha256': digest(manifest), 'state': state})
        return state

    def check(self, manifest, state, context):
        pending, _, _ = unresolved(state['proposals'], state['events'])
        reasons = [{'reason': p['reason'], 'object_id': p['object_id']} for p in pending]
        reasons += [{'reason': 'conflicting_observations'}] if state['conflicts'] else []
        seeds = context['seeds']
        for oid, obj in state['objects'].items():
            if not obj['points']:
                continue
            if obj['identity_support'] == 'low':
                reasons.append({'reason': 'physical_identity_uncertain', 'object_id': oid})
            # A new baseline must not duplicate an old, temporarily unrecognized item.
            if oid not in seeds and any(b['object_id'] == oid for b in state['baselines']):
                rests = [p for p in obj['points'] if p['region'] == 'inside' and p['released'] and p['at_rest']]
                locations = {location_at(manifest, p['media_time_ms']) for p in rests} - {None}
                if any(s['presence'] == 'inside' and s['location'] in locations for s in seeds.values()):
                    reasons.append({'reason': 'existing_item_match_required', 'object_id': oid})
            if oid not in seeds and any(e['object_id'] == oid for e in state['events']):
                if any(s['presence'] == 'outside' and compact(s['name']) == compact(obj['name']) for s in seeds.values()):
                    reasons.append({'reason': 'return_identity_match_required', 'object_id': oid})
        for event in state['events']:
            obj = state['objects'][event['object_id']]
            if event['object_id'] in seeds and not seeds[event['object_id']].get('prototype'):
                reasons.append({'reason': 'identity_reference_missing', 'object_id': event['object_id']})
            if not any(p.get('bbox') and p['region'] == 'inside' and p['at_rest'] and p['released']
                       and p['drawer_phase'] == 'open' and p['media_time_ms'] <= event['event_window_ms'][1]
                       and location_at(manifest, p['media_time_ms']) for p in obj['points']):
                reasons.append({'reason': 'drawer_identity_missing_at_placement', 'object_id': event['object_id']})
        return reasons

    def write_action(self, manifest, state, context, oid, event, identity_map, expected_revision):
        root = Path(manifest['root'])
        baseline = event['kind'] == 'observed_existing'
        key = fingerprint([manifest['id'], oid, event.get('event_id', 'baseline')])[:32]
        operation_id = 'auto-' + key
        with self.store.db.connect() as db:
            row = db.execute('SELECT receipt_json FROM inventory_commits WHERE operation_id=?', (operation_id,)).fetchone()
        if row:
            receipt = json.loads(row[0])
            item_ids = receipt['added_item_ids'] + receipt['updated_item_ids'] + receipt['removed_item_ids'] + receipt['kept_item_ids']
            identity_map[oid] = item_ids[0]
            return receipt
        obj = state['objects'][oid]
        lookup = {f['id']: f for f in manifest['frames']}
        after_ms = event['media_time_ms'] if baseline else event['event_window_ms'][1]
        rests = [p for p in obj['points'] if p.get('bbox') and p['region'] == 'inside'
                 and p['at_rest'] and p['released'] and p['drawer_phase'] == 'open'
                 and p['media_time_ms'] <= after_ms and location_at(manifest, p['media_time_ms'])]
        if not rests:
            raise Conflict('drawer_identity_missing_at_placement')
        point = rests[-1]
        location = location_at(manifest, point['media_time_ms'])
        snapshot = self.store.get_inventory_snapshot(manifest['cabinet_id'], include_removed=True)
        if snapshot['inventory_revision'] != expected_revision:
            raise Conflict('inventory_changed_during_analysis')
        kind = event['kind']
        item_id = identity_map.get(oid)
        if baseline and item_id:
            return None
        if not baseline and kind in ('remove', 'return', 'move') and not item_id:
            raise Conflict('existing_physical_item_identity_missing')
        current = self.store.get_item(item_id) if item_id else None
        if kind == 'add' and current:
            kind = 'return' if current['presence_status'] == 'removed' else 'add'
            if kind == 'add':
                raise Conflict('duplicate_existing_item_insertion')
        if kind == 'remove' and current['location_id'] != location:
            raise Conflict('removal_drawer_disagrees_with_inventory')
        directory = root / 'operations' / key
        directory.mkdir(parents=True, exist_ok=True)
        refs = {point['frame_id']}
        if not baseline:
            refs.update(event['evidence_frame_ids'])
            refs.update([event['before_frame_id'], event['after_frame_id']])
        selected = [lookup[fid] for fid in sorted(refs)]
        for frame in selected:
            target = directory / frame['file']
            target.parent.mkdir(exist_ok=True, parents=True)
            shutil.copyfile(inside_path(root, frame['file']), target)
        ref = 'crop-' + oid
        frame = lookup[point['frame_id']]
        with Image.open(inside_path(root, frame['file'])) as source:
            x1, y1, x2, y2 = point['bbox']
            crop = source.crop((int(x1 * source.width / 1000), int(y1 * source.height / 1000),
                                int(x2 * source.width / 1000), int(y2 * source.height / 1000)))
            if min(crop.size) < 12:
                raise Conflict('item_crop_too_small')
            crop.save(directory / 'item.jpg', quality=94)
        evidence = {ref: {'file': 'item.jpg', 'frame_id': point['frame_id'],
                         'media_time_ms': point['media_time_ms'], 'bbox': point['bbox']}}
        candidate = {**descriptive_identity(obj), 'candidate_id': oid,
                     'location_id': location, 'evidence_refs': [ref] + [f['id'] for f in selected], 'target_kind': 'movable_item',
                     'localization_status': 'usable', 'object_support': obj['identity_support']}
        change = 'kept' if baseline or kind == 'move' else 'remove' if kind == 'remove' else 'add'
        events = [] if baseline else [{**event, 'kind': kind, 'candidate_ids': [oid]}]
        result = {'video': manifest['id'], 'input_sha256': manifest['input_sha256'], 'status': 'completed',
            'inventory_context': snapshot, 'items': [candidate], 'evidence': evidence,
            'registration_kind': 'visible_initial_baseline' if baseline else 'filmed_action',
            'operation_analysis': {'status': 'consistent', 'pending': [],
                'net_changes': [{'candidate_id': oid, 'change': change}], 'event_candidates': events}}
        write_json(directory / 'result.json', result)
        write_json(directory / 'media.json', {'input_sha256': manifest['input_sha256'], 'frames': selected,
            'timestamp_basis': manifest['timestamp_basis'], 'session_id': manifest['id']})
        saved = self.store.import_run(directory, cabinet_id=manifest['cabinet_id'],
            operation_id=operation_id, capture_id=manifest['id'], source_key=VERSION)
        if baseline:
            receipt = self.store.register_visible_baseline(saved['run_id'], oid, location)
        else:
            decision = {'candidate_id': oid, 'action': 'update' if kind == 'move' else kind, 'evidence_refs': [ref]}
            if current:
                decision.update(item_id=current['id'], expected_version=current['version'])
            if kind in ('return', 'move'):
                decision['patch'] = {'location_id': location}
            capture_times = None
            if manifest['timestamp_basis'] == 'host_monotonic_arrival':
                observed = datetime.fromtimestamp(lookup[event['after_frame_id']]['received_unix_ms'] / 1000,
                                                  timezone.utc).isoformat()
                capture_times = {oid: observed}
            receipt = self.store.commit_inventory({**{k: saved[k] for k in ('operation_id', 'run_id', 'base_revision')},
                'idempotency_key': operation_id, 'reviewed_by': 'policy:' + VERSION, 'decisions': [decision]},
                capture_times=capture_times)
        identity_map[oid] = (receipt['added_item_ids'] + receipt['updated_item_ids'] + receipt['removed_item_ids'])[0]
        return receipt

    def process(self, identity):
        session = self.sessions.get(identity)
        root = Path(session['root'])
        manifest = read_json(root / 'session.json')
        if not manifest.get('complete'):
            raise Conflict('incomplete_capture_requires_manual_review')
        if fingerprint({k: manifest[k] for k in ('frames', 'doors', 'epoch')}) != manifest['input_sha256']:
            raise ValueError('Session manifest hash mismatch')
        for frame in manifest['frames']:
            if hashlib.sha256(inside_path(root, frame['file']).read_bytes()).hexdigest() != frame['sha256']:
                raise ValueError('Session evidence hash mismatch')
        context = self.context(manifest, root)
        state = self.analyze(manifest, root, context)
        reasons = self.check(manifest, state, context)
        if reasons:
            self.sessions.update(identity, 'pending', {'reasons': reasons, 'objects': len(state['objects'])})
            return
        mapping = {oid: obj['inventory_item_id'] for oid, obj in context['seeds'].items()}
        revision = context['snapshot']['inventory_revision']
        actions = []
        for baseline in state['baselines']:
            oid = baseline['object_id']
            first_action = min((e['event_window_ms'][1] for e in state['events'] if e['object_id'] == oid),
                               default=float('inf'))
            rests = [p for p in state['objects'][oid]['points'] if p['region'] == 'inside' and p['released']
                     and p['at_rest'] and p['drawer_phase'] == 'open' and p['media_time_ms'] <= first_action
                     and location_at(manifest, p['media_time_ms'])]
            if not rests:
                raise Conflict('drawer_identity_missing_at_baseline')
            baseline = {**baseline, 'media_time_ms': rests[0]['media_time_ms']}
            actions.append((baseline['media_time_ms'], oid, baseline))
        actions += [(e['event_window_ms'][1], e['object_id'], e) for e in state['events']]
        receipts = []
        for _, oid, event in sorted(actions, key=lambda a: a[0]):
            receipt = self.write_action(manifest, state, context, oid, event, mapping, revision)
            if receipt:
                receipts.append(receipt)
                revision = receipt['revision']
                write_json(root / 'receipts.json', receipts)
        if self.store.get_inventory_snapshot(manifest['cabinet_id'])['inventory_revision'] != revision:
            raise Conflict('inventory_changed_during_analysis')
        self.sessions.update(identity, 'committed', {'receipts': receipts, 'events': len(state['events']),
            'inventory_revision': revision, 'no_change': not receipts})

    def run(self, stopped):
        while not stopped.is_set():
            with self.store.db.transaction() as db:
                row = db.execute("SELECT id FROM automatic_sessions WHERE state='queued' ORDER BY created_at,id LIMIT 1").fetchone()
                if row:
                    db.execute("UPDATE automatic_sessions SET state='processing' WHERE id=?", (row['id'],))
            if not row:
                stopped.wait(.5)
                continue
            try:
                self.process(row['id'])
            except Conflict as exc:
                self.sessions.update(row['id'], 'pending', {'reasons': [str(exc)]})
            except Exception as exc:
                # Exception text from transports may contain credentials; keep the UI diagnostic bounded.
                self.sessions.update(row['id'], 'failed', {'reasons': [type(exc).__name__],
                    'message': '识别未完成，画面已保留，可重试。'})
