"""Drawer-relative observations, bounded evidence tools, and deterministic replay."""
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .action_schemas import ScenePlan, StateComparison, Window
from .client import APIError, Client, image_part, write_json
from .config import Settings
from .media import action_frames, video_files
from .matching import AppearanceMatcher
from .schemas import pixel_box

PROMPTS = Path(__file__).parent / 'prompts'
VERSION = 4


def action_prompt(name):
    return (PROMPTS / f'{name}-v3.txt').read_text(encoding='utf-8')


def version_hash():
    sources = [Path(__file__), Path(__file__).with_name('action_schemas.py'),
               Path(__file__).with_name('media.py'), Path(__file__).with_name('schemas.py'),
               Path(__file__).with_name('matching.py')] + sorted(PROMPTS.glob('*-v3.txt'))
    return hashlib.sha256(b''.join(p.read_bytes() for p in sources)).hexdigest()


def evenly(frames, limit):
    if len(frames) <= limit:
        return frames
    return [frames[round(i * (len(frames) - 1) / (limit - 1))] for i in range(limit)]


def frame_parts(frames, root):
    parts, root = [], Path(root)
    directory = root / 'storyboards'
    directory.mkdir(exist_ok=True)
    for start in range(0, len(frames), 4):
        group = frames[start:start + 4]
        key = hashlib.sha256('|'.join(f['id'] for f in group).encode()).hexdigest()[:16]
        page = Image.new('RGB', (1280, 792), 'white')
        draw, font = ImageDraw.Draw(page), ImageFont.load_default(size=22)
        for index, f in enumerate(group):
            with Image.open(root / f.get('matched_file', f['file'])) as source:
                im = source.copy()
            im.thumbnail((640, 360))
            x, y = index % 2 * 640, index // 2 * 396
            page.paste(im, (x, y))
            draw.text((x + 5, y + 362), f'{f["id"]}  {f["media_time_ms"]}ms', fill='black', font=font)
        filename = directory / (key + '.jpg')
        page.save(filename, quality=95)
        parts += [{'type': 'text', 'text': '时序拼图：左上→右上→左下→右下，帧清单：' + json.dumps(
            [{k: f[k] for k in ('id', 'media_time_ms')} for f in group])}, image_part(filename)]
    return parts


def snapshot_parts_for_pair(before, after, plan, frames, root, prefix):
    parts, root = [], Path(root)
    for label, frame in (('before', before), ('after', after)):
        if not frame:
            parts.append({'type': 'text', 'text': label + ': no valid snapshot'})
            continue
        anchors = [a for a in (plan.before_snapshot, plan.after_snapshot) if a]
        anchor = min(anchors, key=lambda a: abs(frames[a.frame_id]['media_time_ms'] - frame['media_time_ms'])) if anchors else None
        parts.append({'type': 'text', 'text': f'{label}: frame_id={frame["id"]}; media_time_ms={frame["media_time_ms"]}; ROI then full frame'})
        if anchor:
            with Image.open(root / frame['file']) as im:
                crop = im.crop(pixel_box(anchor.bbox, *im.size, padding=0))
            destination = root / 'snapshots' / f'{prefix}-{label}.jpg'
            destination.parent.mkdir(exist_ok=True)
            crop.save(destination, quality=95)
            parts.append(image_part(destination))
        parts.append(image_part(root / frame.get('matched_file', frame['file'])))
    return parts


def coverage_windows(plan, frames):
    """Short coverage windows prevent a scene planner silently skipping interactions."""
    start = frames[0]['media_time_ms']
    end = frames[-1]['media_time_ms']
    if plan.after_snapshot:
        end = next(f['media_time_ms'] for f in frames if f['id'] == plan.after_snapshot.frame_id)
    result = []
    while start < end:
        stop = min(end, start + 1250)
        a = min(frames, key=lambda f: abs(f['media_time_ms'] - start))
        b = min(frames, key=lambda f: abs(f['media_time_ms'] - stop))
        if a['id'] != b['id']:
            result.append(Window(window_id=f'c{len(result) + 1}', start_frame_id=a['id'],
                end_frame_id=b['id'], reason='bounded temporal coverage; scene suggestions are not action truth'))
        start = stop
    if len(result) > 48:
        raise ValueError('Interaction call budget exceeded; split the recording into operations')
    return result


def validate_plan(plan, frames, allowed):
    def interval(start, end, strict=False):
        if start not in allowed or end not in allowed:
            raise ValueError('Scene references a frame not supplied to this stage')
        a, b = frames[start]['media_time_ms'], frames[end]['media_time_ms']
        if a > b or (strict and a == b):
            raise ValueError('Scene interval must increase')
        return a, b

    previous = -1
    for phase in plan.phases:
        a, b = interval(phase.start_frame_id, phase.end_frame_id)
        if a < previous:
            raise ValueError('Scene phases overlap or are out of order')
        previous = b
    for anchor in (plan.before_snapshot, plan.after_snapshot):
        if anchor:
            if anchor.frame_id not in allowed:
                raise ValueError('Snapshot references an unknown frame')
            t = frames[anchor.frame_id]['media_time_ms']
            if not any(p.phase == 'open' and p.interior_visible and
                       frames[p.start_frame_id]['media_time_ms'] <= t <= frames[p.end_frame_id]['media_time_ms']
                       for p in plan.phases):
                raise ValueError('Snapshot must be in an open, visible drawer phase')
    if plan.before_snapshot and plan.after_snapshot:
        interval(plan.before_snapshot.frame_id, plan.after_snapshot.frame_id, strict=True)
    previous, ids = -1, set()
    for window in plan.windows:
        a, _ = interval(window.start_frame_id, window.end_frame_id, strict=True)
        if a < previous or window.window_id in ids:
            raise ValueError('Interaction windows must have unique IDs and increasing starts')
        previous = a
        ids.add(window.window_id)
    if plan.windows and plan.before_snapshot:
        before_time = frames[plan.before_snapshot.frame_id]['media_time_ms']
        first_window = frames[plan.windows[0].start_frame_id]['media_time_ms']
        if before_time > first_window:
            raise ValueError('Before snapshot must not be after the first interaction window')
    if plan.windows and plan.after_snapshot:
        after_time = frames[plan.after_snapshot.frame_id]['media_time_ms']
        last_window = frames[plan.windows[-1].end_frame_id]['media_time_ms']
        if after_time < last_window:
            raise ValueError('After snapshot must not be before the last interaction window')


def validate_states(value, candidate_ids, allowed_before, allowed_after, frames):
    ids = [v.candidate_id for v in value.objects]
    if set(ids) != set(candidate_ids) or len(ids) != len(candidate_ids):
        raise ValueError(f'State objects must cover exactly candidate_ids={sorted(candidate_ids)}; received={ids}')
    for obj in value.objects:
        for state, allowed in ((obj.before, allowed_before), (obj.after, allowed_after)):
            if not set(state.frame_ids) <= set(allowed):
                raise ValueError('State cites frames outside its supplied context')
            if state.location not in ('unknown', 'occluded') and not state.frame_ids:
                raise ValueError('Known state requires frame evidence')
            if state.location == 'absent' and not state.interior_clear:
                raise ValueError('Absence requires a clear view of the drawer interior')
        if obj.before.frame_ids and obj.after.frame_ids:
            if max(frames[f]['media_time_ms'] for f in obj.before.frame_ids) >= min(frames[f]['media_time_ms'] for f in obj.after.frame_ids):
                raise ValueError('Object state evidence must have strictly ordered before/after references')


def guard_states(value, rejected):
    for obj in value.objects:
        for side in ('before', 'after'):
            state = getattr(obj, side)
            if state.location == 'absent' and not state.interior_clear:
                rejected.append({'candidate_id': obj.candidate_id, 'side': side,
                                 'original': state.model_dump(), 'reason': 'absence_without_visibility'})
                state.location = 'unknown'
    return value


ACTION_TOOLS = [
    {'type': 'function', 'function': {'name': 'get_frames',
     'description': 'Read original-video evidence for a bounded media-time range. No database or inventory writes.',
     'parameters': {'type': 'object', 'properties': {'start_ms': {'type': 'integer', 'minimum': 0},
         'end_ms': {'type': 'integer', 'minimum': 0}, 'fps': {'type': 'integer', 'minimum': 1, 'maximum': 24}},
         'required': ['start_ms', 'end_ms', 'fps'], 'additionalProperties': False}}},
    {'type': 'function', 'function': {'name': 'get_object_evidence',
     'description': 'Read visual prototypes for one candidate from this run. These do not establish initial inventory.',
     'parameters': {'type': 'object', 'properties': {'candidate_id': {'type': 'string'}},
                    'required': ['candidate_id'], 'additionalProperties': False}}},
    {'type': 'function', 'function': {'name': 'get_inventory_snapshot',
     'description': 'Read explicitly supplied initial inventory. Returns unavailable when none was supplied; never reads ans.md.',
     'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False}}}
]


class EvidenceTools:
    def __init__(self, video, output, source, result, frames, initial_inventory=None):
        self.video, self.output, self.source = Path(video), Path(output), Path(source)
        self.result, self.frames = result, {f['id']: f for f in frames}
        self.initial_inventory, self.trace = initial_inventory, []
        self.end_ms = frames[-1]['media_time_ms']
        self.scope = None
        self.appearance = AppearanceMatcher(result, source)

    def prototypes(self, candidate_id=None, include_images=True):
        parts = []
        for item in self.result['items']:
            if candidate_id and item['candidate_id'] != candidate_id:
                continue
            parts.append({'type': 'text', 'text': '物品样例（非本次状态证据）：' + json.dumps(
                {k: item.get(k) for k in ('candidate_id', 'name', 'category', 'description', 'localization_status')}, ensure_ascii=False)})
            for ref in item.get('evidence_refs', [])[:1] if include_images else []:
                parts.append(image_part(self.source / self.result['evidence'][ref]['file']))
        return parts

    def call(self, name, args, origin='orchestrator'):
        images = []
        try:
            if not isinstance(args, dict):
                raise ValueError('arguments must be an object')
            if name == 'get_frames':
                if set(args) != {'start_ms', 'end_ms', 'fps'} or any(type(v) is not int for v in args.values()):
                    raise ValueError('expected integer start_ms/end_ms/fps')
                a, b, fps = args['start_ms'], args['end_ms'], args['fps']
                lo, hi = self.scope or (0, self.end_ms)
                if a < lo - 250 or b > hi + 250 or b - a > 15000 or not 1 <= fps <= 24:
                    raise ValueError('invalid or excessive video evidence range')
                a, b = max(lo, a), min(hi, b)
                if not 0 <= a < b <= self.end_ms:
                    raise ValueError('invalid video evidence range after boundary normalization')
                frames = action_frames(self.video, self.output, fps=fps, start_ms=a, end_ms=b, max_frames=400)
                self.frames.update({f['id']: f for f in frames})
                selected = evenly(frames, max(2, 20 - len(self.result['items'])))
                for frame in selected:
                    self.appearance.annotate(frame, self.output)
                result = {'status': 'ok', 'frames': selected, 'decoded_samples': len(frames),
                          'returned_samples': len(selected), 'sampling_fps': fps,
                          'effective_range_ms': [a, b],
                          'note': 'If decimated, request a narrower range for finer temporal evidence.'}
                images = frame_parts(selected, self.output)
            elif name == 'get_object_evidence':
                if set(args) != {'candidate_id'} or args['candidate_id'] not in {i['candidate_id'] for i in self.result['items']}:
                    raise ValueError('unknown candidate')
                result = {'status': 'ok', 'candidate_id': args['candidate_id'], 'kind': 'prototype_not_inventory'}
                images = self.prototypes(args['candidate_id'])
            elif name == 'get_inventory_snapshot':
                if args:
                    raise ValueError('inventory snapshot accepts no arguments')
                result = {'status': 'provided', 'inventory': self.initial_inventory} if self.initial_inventory is not None else {
                    'status': 'unavailable', 'inventory': None, 'database': 'not_connected',
                    'note': 'Unknown initial inventory is not an empty drawer.'}
            else:
                raise ValueError('tool is not in the read-only whitelist')
        except (ValueError, TypeError) as exc:
            result = {'status': 'rejected', 'reason': str(exc)}
        self.trace.append({'origin': origin, 'tool': name, 'arguments': args, 'result': result})
        return result, images


def presence(state):
    if state['location'] == 'inside':
        return True
    if state['location'] == 'outside' or (state['location'] == 'absent' and state['interior_clear']):
        return False
    return None


def _time_bounds(state, frames):
    if not frames or not state.get('frame_ids'):
        return None
    times = [frames[frame_id]['media_time_ms'] for frame_id in state['frame_ids'] if frame_id in frames]
    return (min(times), max(times)) if times else None


def _endpoint_replacement(side, old, revised, frames):
    """Allow a review to move a before endpoint earlier or an after endpoint later."""
    old_bounds, new_bounds = _time_bounds(old, frames), _time_bounds(revised, frames)
    if not old_bounds or not new_bounds:
        return False
    if side == 'before':
        return new_bounds[1] < old_bounds[0]
    return new_bounds[0] > old_bounds[1]


def review_conflicts(original, revised, frames=None):
    """A later answer is not independent proof that a known observation was wrong."""
    conflicts = []
    previous = {o['candidate_id']: o for o in original['objects']}
    for obj in revised['objects']:
        old = previous.get(obj['candidate_id'])
        if not old:
            continue
        for side in ('before', 'after'):
            a, b = old[side], obj[side]
            pa, pb = presence(a), presence(b)
            if pa is not None and pb is not None and pa != pb:
                if _endpoint_replacement(side, a, b, frames):
                    continue
                conflicts.append({'candidate_id': obj['candidate_id'], 'side': side,
                                  'reason': 'review_reversed_known_presence'})
            elif (pa is True and pb is True and a['at_rest'] != b['at_rest']
                  and (set(a['frame_ids']) & set(b['frame_ids'])
                       or not _endpoint_replacement(side, a, b, frames))):
                conflicts.append({'candidate_id': obj['candidate_id'], 'side': side,
                                  'reason': 'review_reversed_same_frame_rest_state'})
    return conflicts


def selected_observation(window, frames=None):
    original = window.get('initial_observation')
    revised = window.get('review_observation', window['observation'])
    conflicts = review_conflicts(original, revised, frames) if original else []
    return (original if conflicts else window['observation']), conflicts


def _box_overlap(first, second):
    ax1, ay1, ax2, ay2 = first
    bx1, by1, bx2, by2 = second
    ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
    intersection = max(0, ix2 - ix1) * max(0, iy2 - iy1)
    first_area = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    second_area = max(0, bx2 - bx1) * max(0, by2 - by1)
    if not intersection or not first_area or not second_area:
        return 0.0, 0.0
    return intersection / max(first_area, second_area), intersection / min(first_area, second_area)


def instance_conflicts(observation, frames):
    """Return strong same-frame appearance overlaps that make identity non-exclusive."""
    conflicts = []
    for frame_id in set(frame_id for obj in observation['objects']
                        for side in ('before', 'after') for frame_id in obj[side]['frame_ids']):
        cited = {obj['candidate_id'] for obj in observation['objects']
                 if any(frame_id in obj[side]['frame_ids'] for side in ('before', 'after'))}
        hits = [hit for hit in frames.get(frame_id, {}).get('appearance_hints', [])
                if hit.get('candidate_id') in cited and len(hit.get('bbox_pixels', [])) == 4]
        for index, first in enumerate(hits):
            for second in hits[index + 1:]:
                if first['candidate_id'] == second['candidate_id']:
                    continue
                iou, containment = _box_overlap(first['bbox_pixels'], second['bbox_pixels'])
                if iou >= 0.45 or containment >= 0.75:
                    conflicts.append({'frame_id': frame_id,
                                      'candidate_ids': sorted([first['candidate_id'], second['candidate_id']]),
                                      'reason': 'overlapping_instance_hints'})
    unique = {(item['frame_id'], tuple(item['candidate_ids'])): item for item in conflicts}
    return list(unique.values())


def reconcile(snapshots, windows, frames):
    """Replay dated evidence and complete a placement only after stable support."""
    def time_of(state):
        return max((frames[f]['media_time_ms'] for f in state['frame_ids']), default=-1)

    def supported(obj):
        return obj['identity'] == 'supported' and obj['support'] in ('high', 'medium')

    observations = [(w, *selected_observation(w, frames)) for w in windows]
    baseline, final, prefixes = {}, {}, []
    snapshot_conflicts = instance_conflicts(snapshots, frames)
    blocked_candidates = {cid for conflict in snapshot_conflicts for cid in conflict['candidate_ids']}
    for snapshot in snapshots['objects']:
        cid = snapshot['candidate_id']
        choices = [(snapshot, 'snapshot', None, 'before')] + [
            (o, 'window', w['window_id'], 'before') for w, observation, _ in observations
            for o in observation['objects'] if o['candidate_id'] == cid]
        choices = [(o, source, wid, side) for o, source, wid, side in choices
                   if supported(o) and presence(o['before']) is not None and o['before']['frame_ids']]
        if supported(snapshot) and presence(snapshot['before']) is True:
            choices += [(o, 'window_after', w['window_id'], 'after')
                        for w, observation, _ in observations for o in observation['objects']
                        if o['candidate_id'] == cid and supported(o) and presence(o['before']) is None
                        and presence(o['after']) is True and o['after']['at_rest'] and o['after']['frame_ids']]
        if choices:
            obj, source, wid, side = min(choices, key=lambda row: time_of(row[0][row[3]]))
            baseline[cid] = {'state': obj[side], 'identity': obj['identity'], 'support': obj['support'],
                             'source': source, 'window_id': wid, 'media_time_ms': time_of(obj[side])}
            if side == 'after':
                prefixes.append({'candidate_id': cid, 'until_ms': time_of(obj[side]),
                                 'reason': 'initial_presence_unobserved_before_first_clear_view'})
        final[cid] = presence(snapshot['after']) if supported(snapshot) else None
    initial = {o['candidate_id']: presence(baseline[o['candidate_id']]['state'])
               if o['candidate_id'] in baseline else None for o in snapshots['objects']}
    ledger, removed, events, pending, gaps = dict(initial), set(), [], [], []
    pending.extend({'candidate_ids': conflict['candidate_ids'], 'frame_id': conflict['frame_id'],
                    'reason': conflict['reason']} for conflict in snapshot_conflicts)
    known_refs = {cid: list(baseline[cid]['state']['frame_ids']) if cid in baseline else [] for cid in initial}
    known_states = {cid: baseline[cid]['state'] if cid in baseline else None for cid in initial}
    intents, completions, cancelled = {}, [], []

    def emit(obj, window, before_state, bridged=False, origin=None):
        cid = obj['candidate_id']
        before_refs, after_refs = before_state['frame_ids'], obj['after']['frame_ids']
        if (not before_refs or not after_refs or time_of(before_state) >=
                min(frames[f]['media_time_ms'] for f in after_refs)):
            pending.append({'window_id': window['window_id'], 'candidate_id': cid,
                            'reason': 'transition_has_no_time_interval'})
            return False
        after = presence(obj['after'])
        kind = ('return' if cid in removed else 'add') if after else 'remove'
        a = min(before_refs, key=lambda f: frames[f]['media_time_ms'])
        b = max(after_refs, key=lambda f: frames[f]['media_time_ms'])
        events.append({'kind': kind, 'candidate_ids': [cid], 'before_frame_id': a, 'after_frame_id': b,
            'event_window_ms': [frames[a]['media_time_ms'], frames[b]['media_time_ms']],
            'explanation': before_state['description'] + ' → ' + obj['after']['description'],
            'support': 'medium' if origin and origin['support'] == 'medium' else obj['support'],
            'evidence_type': (origin or {}).get('evidence', obj['evidence']),
            'window_id': window['window_id'], 'bridged_from_prior_evidence': bridged,
            'placement_completed_in_later_window': origin is not None and origin['window_id'] != window['window_id'],
            'state_evidence': {'before': before_state, 'after': obj['after']},
            'evidence_window_ids': list(dict.fromkeys([(origin or {}).get('window_id', window['window_id']), window['window_id']])),
            'committed': False, 'stored_at': None, 'certainty': 'estimated', 'source': 'vision_state_transition'})
        ledger[cid], known_refs[cid] = after, list(after_refs)
        known_states[cid] = obj['after']
        if after:
            removed.discard(cid)
        else:
            removed.add(cid)
        return True

    for window, observation, conflicts in observations:
        pending.extend({**conflict, 'window_id': window['window_id']} for conflict in conflicts)
        window_conflicts = instance_conflicts(observation, frames)
        pending.extend({**conflict, 'window_id': window['window_id']} for conflict in window_conflicts)
        blocked = blocked_candidates | {cid for conflict in window_conflicts for cid in conflict['candidate_ids']}
        for obj in observation['objects']:
            cid = obj['candidate_id']
            if cid in blocked:
                continue
            before, after = presence(obj['before']), presence(obj['after'])
            if not supported(obj):
                if before == after:
                    gaps.append({'window_id': window['window_id'], 'candidate_id': cid, 'reason': 'uncertain_unchanged_identity'})
                else:
                    pending.append({'window_id': window['window_id'], 'candidate_id': cid,
                                    'reason': 'identity_or_transition_evidence_uncertain'})
                continue
            if cid in baseline and any(
                    presence(obj[side]) is not None and presence(obj[side]) != initial[cid]
                    and set(obj[side]['frame_ids']) & set(baseline[cid]['state']['frame_ids'])
                    for side in ('before', 'after')):
                pending.append({'window_id': window['window_id'], 'candidate_id': cid,
                                'reason': 'same_frame_presence_conflict'})
                continue
            if before is None and after is None:
                gaps.append({'window_id': window['window_id'], 'candidate_id': cid, 'reason': 'not_observed'})
                continue
            if (before is None and after is True and cid in baseline
                    and baseline[cid]['source'] == 'window_after'
                    and time_of(obj['after']) == baseline[cid]['media_time_ms']):
                gaps.append({'window_id': window['window_id'], 'candidate_id': cid,
                             'reason': 'first_clear_presence_not_an_add_event'})
                continue
            usable = obj['evidence'] in ('crossing', 'comparable_states') and not observation['drawer_only']
            intent = intents.get(cid)
            # A held object may span several windows before the drawer supports it.
            if intent and usable and after is True and obj['after']['at_rest'] and time_of(obj['after']) > intent['seen_ms']:
                if emit(obj, window, intent['before'], intent['bridged'], origin=intent):
                    completions.append({'candidate_id': cid, 'started_window_id': intent['window_id'],
                                        'completed_window_id': window['window_id']})
                    del intents[cid]
                continue
            if intent and usable and after is False:
                if before is True and obj['before']['at_rest']:
                    placed = {**obj, 'after': obj['before']}
                    if emit(placed, window, intent['before'], intent['bridged'], origin=intent):
                        del intents[cid]
                elif before is not None and not obj['before']['at_rest']:
                    cancelled.append({'candidate_id': cid, 'window_id': window['window_id'],
                                      'reason': 'held_object_left_without_confirmed_placement'})
                    del intents[cid]
                    ledger[cid], known_refs[cid] = False, list(obj['after']['frame_ids'])
                    known_states[cid] = obj['after']
                    continue
            before_state, bridged = obj['before'], False
            if before is None and after is not None and ledger[cid] is not None and known_refs[cid]:
                if max(frames[f]['media_time_ms'] for f in known_refs[cid]) < min(
                        (frames[f]['media_time_ms'] for f in obj['after']['frame_ids']), default=-1):
                    before, bridged = ledger[cid], True
                    before_state = known_states[cid]
            if before == after and before is not None:
                if ledger[cid] is not None and ledger[cid] != before:
                    if intent and before is True:
                        continue
                    if (before is True and usable and (not obj['before']['at_rest'] or not obj['after']['at_rest'])
                            and known_refs[cid] and obj['after']['frame_ids']
                            and max(frames[f]['media_time_ms'] for f in known_refs[cid]) < min(
                                frames[f]['media_time_ms'] for f in obj['after']['frame_ids'])):
                        intent = {'before': known_states[cid], 'bridged': True, 'window_id': window['window_id'],
                                  'seen_ms': time_of(obj['after']), 'evidence': obj['evidence'], 'support': obj['support']}
                        if obj['after']['at_rest']:
                            emit(obj, window, intent['before'], True, origin=intent)
                        else:
                            intents[cid] = intent
                        continue
                    pending.append({'window_id': window['window_id'], 'candidate_id': cid,
                                    'reason': 'unchanged_observation_conflicts_with_history'})
                elif obj['after']['frame_ids'] and time_of(obj['after']) >= max(
                        (frames[f]['media_time_ms'] for f in known_refs[cid]), default=-1):
                    known_refs[cid] = list(obj['after']['frame_ids'])
                    known_states[cid] = obj['after']
                continue
            reason = None
            if obj['evidence'] not in ('crossing', 'comparable_states'):
                reason = 'identity_or_transition_evidence_uncertain'
            elif before is None or after is None:
                reason = 'occlusion_or_unknown_state'
            elif observation['drawer_only']:
                reason = 'drawer_only_conflicts_with_object_transition'
            elif ledger[cid] is not None and ledger[cid] != before:
                prior = next((e for e in reversed(events) if cid in e['candidate_ids']), None)
                times = [frames[f]['media_time_ms'] for f in obj['before']['frame_ids'] + obj['after']['frame_ids']]
                same_direction = prior and ((after and prior['kind'] in ('add', 'return')) or (not after and prior['kind'] == 'remove'))
                if times and same_direction and min(times) <= prior['event_window_ms'][1] and max(times) >= prior['event_window_ms'][0]:
                    continue
                reason = 'transition_conflicts_with_previous_state'
            if reason:
                pending.append({'window_id': window['window_id'], 'candidate_id': cid, 'reason': reason})
                continue
            if after and not obj['after']['at_rest']:
                if cid not in intents:
                    intents[cid] = {'before': before_state, 'bridged': bridged, 'window_id': window['window_id'],
                                    'seen_ms': time_of(obj['after']), 'evidence': obj['evidence'], 'support': obj['support']}
                continue
            emit(obj, window, before_state, bridged)
    pending.extend({'candidate_id': cid, 'window_id': intent['window_id'], 'reason': 'placement_not_completed'}
                   for cid, intent in intents.items())
    events.sort(key=lambda e: (e['event_window_ms'][1], e['event_window_ms'][0]))
    checkpoints = []
    for obj in snapshots['objects']:
        cid, checkpoint = obj['candidate_id'], obj['before']
        if cid not in baseline or not supported(obj) or presence(checkpoint) is None:
            continue
        t = time_of(checkpoint)
        if t <= baseline[cid]['media_time_ms']:
            continue
        relevant = [e for e in events if cid in e['candidate_ids']]
        if any(e['event_window_ms'][0] <= t < e['event_window_ms'][1] for e in relevant):
            checkpoints.append({'candidate_id': cid, 'media_time_ms': t, 'status': 'during_transition'})
            continue
        earlier = [e for e in relevant if e['event_window_ms'][1] <= t]
        expected = earlier[-1]['kind'] != 'remove' if earlier else initial[cid]
        matches = expected == presence(checkpoint)
        checkpoints.append({'candidate_id': cid, 'media_time_ms': t, 'status': 'matched' if matches else 'conflict'})
        if not matches:
            pending.append({'candidate_id': cid, 'reason': 'snapshot_checkpoint_conflicts_with_history'})
    net = []
    for cid in initial:
        a, b = initial[cid], final[cid]
        if a is None or b is None:
            pending.append({'candidate_id': cid, 'reason': 'initial_or_final_snapshot_unknown'})
            change = 'unknown'
        else:
            change = 'kept' if a and b else 'add' if b else 'remove' if a else 'not_present'
            if ledger[cid] != b:
                pending.append({'candidate_id': cid, 'reason': 'history_does_not_match_final_snapshot',
                                'history_present': ledger[cid], 'snapshot_present': b})
        net.append({'candidate_id': cid, 'change': change, 'initial_present': a, 'final_present': b,
                    'committed': False})
    return {'status': 'consistent' if not pending else 'pending',
            'history_status': 'consistent' if not pending else 'pending',
            'net_change_status': 'supported_endpoints' if all(n['change'] != 'unknown' for n in net) else 'unknown',
            'event_candidates': events, 'net_changes': net, 'pending': pending, 'observation_gaps': gaps,
            'baseline_observations': baseline, 'snapshot_checkpoints': checkpoints,
            'unobserved_prefixes': prefixes,
            'placement_completions': completions, 'cancelled_placements': cancelled,
            'note': 'Consistency of visual evidence is not ground-truth correctness or a database commit.'}


def analyze_operation(video, output, client, result, structured, source=None, initial_inventory=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    frames_list = action_frames(video, output)
    tools = EvidenceTools(video, output, source or output, result, frames_list, initial_inventory)
    for f in frames_list:
        tools.appearance.annotate(f, output)
    write_json(output / 'appearance-matches.json', {f['id']: f['appearance_hints'] for f in frames_list})
    write_json(output / 'action-media.json', {'version': VERSION, 'sampling_fps': 8, 'frames': frames_list,
        'retained_actual_final_frame': True, 'source_video': Path(video).name})
    overview = evenly(frames_list, min(20, max(2, len(frames_list) // 2 + 1)))
    inventory, _ = tools.call('get_inventory_snapshot', {})
    plan_parts = [{'type': 'text', 'text': '已知初始库存（如有）：' + json.dumps(inventory, ensure_ascii=False)}]
    plan_parts += tools.prototypes(include_images=False) + frame_parts(overview, output)
    rejected_states = []

    def check_states(value, cids, before, after):
        guard_states(value, rejected_states)
        validate_states(value, cids, before, after, tools.frames)

    try:
        plan = structured(client, [{'role': 'system', 'content': action_prompt('scene')},
            {'role': 'user', 'content': plan_parts}], 'scene_' + Path(video).stem, ScenePlan,
            lambda v: validate_plan(v, tools.frames, {f['id'] for f in overview}), max_tokens=5000)
        write_json(output / 'scene-plan.json', plan.model_dump())
        snapshot_parts, before_ids, after_ids = [], [], []
        for label, anchor, allowed in (('before', plan.before_snapshot, before_ids), ('after', plan.after_snapshot, after_ids)):
            if not anchor:
                snapshot_parts.append({'type': 'text', 'text': label + ': no valid snapshot'})
                continue
            f = tools.frames[anchor.frame_id]
            allowed.append(f['id'])
            with Image.open(output / f['file']) as im:
                box = pixel_box(anchor.bbox, *im.size, padding=0)
                crop = im.crop(box)
            (output / 'snapshots').mkdir(exist_ok=True)
            crop_path = output / f'snapshots/{label}.jpg'
            crop.save(crop_path, quality=95)
            snapshot_parts += [{'type': 'text', 'text': f'{label}: frame_id={f["id"]}; drawer ROI first, full frame second'},
                               image_part(crop_path), image_part(output / f.get('matched_file', f['file']))]
        cids = {i['candidate_id'] for i in result['items']}
        contract = {'type': 'text', 'text': '输出objects必须覆盖全部candidate_ids=' + json.dumps(sorted(cids)) +
                    '。未见的候选也要用unknown/absent/occluded描述；identity只允许supported或uncertain；support只允许high/medium/low。'}
        snapshots = structured(client, [{'role': 'system', 'content': action_prompt('snapshots')},
            {'role': 'user', 'content': [contract] + tools.prototypes(include_images=False) + snapshot_parts}], 'snapshots_' + Path(video).stem,
            StateComparison, lambda v: check_states(v, cids, before_ids, after_ids), max_tokens=5200).model_dump()
        write_json(output / 'snapshot-states.json', snapshots)
        windows = []
        clip_ranges = {}
        coverage = coverage_windows(plan, frames_list)
        write_json(output / 'coverage-windows.json', [w.model_dump() for w in coverage])
        for index, window in enumerate(coverage):
            a = tools.frames[window.start_frame_id]['media_time_ms']
            b = tools.frames[window.end_frame_id]['media_time_ms']
            a, b = max(0, a - 250), min(tools.end_ms, b + 250)
            if plan.after_snapshot:
                b = min(b, tools.frames[plan.after_snapshot.frame_id]['media_time_ms'])
            if index == 0 and plan.before_snapshot:
                a = min(a, tools.frames[plan.before_snapshot.frame_id]['media_time_ms'])
            if index == len(coverage) - 1 and plan.after_snapshot:
                b = max(b, tools.frames[plan.after_snapshot.frame_id]['media_time_ms'])
            clip_ranges[window.window_id] = (a, b)
            clip, images = tools.call('get_frames', {'start_ms': a, 'end_ms': b, 'fps': 8})
            if clip['status'] != 'ok':
                raise ValueError('Interaction clip unavailable: ' + clip.get('reason', 'unknown'))
            allowed = {f['id'] for f in clip['frames']}
            first, last = clip['frames'][0], clip['frames'][-1]
            checkpoint_parts = snapshot_parts_for_pair(first, last, plan, tools.frames, output, window.window_id)
            messages = [{'role': 'system', 'content': action_prompt('states')}, {'role': 'user', 'content':
                [contract] + tools.prototypes(include_images=False) + checkpoint_parts + images}]
            observation = structured(client, messages, 'states_' + Path(video).stem + '_' + window.window_id,
                StateComparison, lambda v: check_states(v, cids, allowed, allowed), max_tokens=5600).model_dump()
            windows.append({**window.model_dump(), 'observation': observation,
                            'supplied_frame_ids': [f['id'] for f in clip['frames']],
                            'input_kind': 'timestamped_sequence_and_endpoints'})
            write_json(output / 'window-states.json', windows)
        analysis = reconcile(snapshots, windows, tools.frames)
        # One bounded, real function-calling round to inspect unresolved interactions.
        unresolved = {p.get('window_id') for p in analysis['pending']} - {None}
        if any(p['reason'] == 'history_does_not_match_final_snapshot' for p in analysis['pending']):
            unresolved.update(w['window_id'] for w in windows)
        review_attempts = 0
        for window in windows:
            if not analysis['pending']:
                break
            if review_attempts >= 3:
                break
            if window['window_id'] not in unresolved:
                continue
            mismatch_ids = {p['candidate_id'] for p in analysis['pending'] if p['reason'] == 'history_does_not_match_final_snapshot'}
            targets = {p['candidate_id'] for p in analysis['pending'] if p.get('window_id') == window['window_id']} | mismatch_ids
            if not targets:
                continue
            targeted_contract = {'type': 'text', 'text': '本次只复核candidate_ids=' + json.dumps(sorted(targets)) +
                '。objects恰好覆盖这些ID；identity只允许supported/uncertain。样例不等于目标实际出现，不要把另一白色包装当成两件物品。'}
            request = [{'role': 'system', 'content': action_prompt('states') + '\n你正在复核歧义片段，请调用get_frames读取更密或更窄的原视频证据，再重新观察。'},
                {'role': 'user', 'content': [targeted_contract] + tools.prototypes(next(iter(targets)) if len(targets) == 1 else None, include_images=False) + [{'type': 'text', 'text': json.dumps({
                    'window_id': window['window_id'],
                    'pending': analysis['pending'], 'video_end_ms': tools.end_ms,
                    'window_start_ms': clip_ranges[window['window_id']][0],
                    'window_end_ms': clip_ranges[window['window_id']][1]}, ensure_ascii=False)}]}]
            response = client.chat(request, 'action_tool_' + Path(video).stem + '_' + window['window_id'],
                tools=ACTION_TOOLS, tool_choice={'type': 'function', 'function': {'name': 'get_frames'}}, max_tokens=1200)
            review_attempts += 1
            msg = response['choices'][0]['message']
            calls = msg.get('tool_calls') or []
            if len(calls) != 1:
                continue
            request.append({k: v for k, v in msg.items() if k in ('role', 'content', 'tool_calls')})
            supplied, added_images = set(), []
            for call in calls:
                try:
                    args = json.loads(call['function']['arguments'])
                except (ValueError, KeyError, TypeError):
                    args = None
                tools.scope = clip_ranges[window['window_id']]
                tool_result, extra = tools.call(call['function']['name'], args, origin='native_function_call')
                tools.scope = None
                if tool_result['status'] == 'rejected':
                    a, b = clip_ranges[window['window_id']]
                    tool_result, extra = tools.call('get_frames', {'start_ms': a, 'end_ms': b, 'fps': 12},
                                                    origin='orchestrated_tool_repair')
                    tool_result = {**tool_result, 'requested_arguments_rejected': True}
                supplied.update(f['id'] for f in tool_result.get('frames', []))
                added_images += extra
                request.append({'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(tool_result, ensure_ascii=False)})
            if supplied:
                request.append({'role': 'user', 'content': added_images + [{'type': 'text', 'text': '现在重新观察此片段，返回约定状态JSON。'}]})
                window.setdefault('initial_observation', window['observation'])
                revised = structured(client, request,
                    'states_recheck_' + Path(video).stem + '_' + window['window_id'], StateComparison,
                    lambda v: check_states(v, targets, supplied, supplied), max_tokens=5600).model_dump()
                window['review_observation'] = revised
                conflicts = review_conflicts(window['initial_observation'], revised, tools.frames)
                window['review_decision'] = {'status': 'pending' if conflicts else 'accepted', 'conflicts': conflicts}
                if not conflicts:
                    window['observation'] = {**revised, 'objects':
                        [o for o in window['observation']['objects'] if o['candidate_id'] not in targets] + revised['objects']}
                analysis = reconcile(snapshots, windows, tools.frames)
        analysis.update(scene_plan=plan.model_dump(), version=VERSION,
                        review_budget={'limit': 3, 'used': review_attempts},
                        rejected_state_claims=rejected_states,
                        snapshot_refs={'before': before_ids, 'after': after_ids},
                        database={'status': 'snapshot_read' if initial_inventory is not None else 'not_connected',
                                  'inventory_commits': 0})
        write_json(output / 'window-states.json', windows)
        write_json(output / 'operation-analysis.json', analysis)
        return analysis
    finally:
        write_json(output / 'rejected-state-claims.json', rejected_states)
        write_json(output / 'action-tool-trace.json', {'trace': tools.trace})


def action_batch(input_path, source_run, output, names=None, resume=False, database=None, cabinet_id='cabinet-01', model=None):
    """Reuse frozen recognition, but re-read original video for all action evidence."""
    from .pipeline import digest, render_report, structured

    source_run, output = Path(source_run).resolve(), Path(output).resolve()
    if source_run == output or source_run in output.parents or output in source_run.parents:
        raise ValueError('Action experiment output must be separate from the source run')
    settings = Settings.load()
    if model:
        settings.model = model
    client = Client(settings, output)
    if database is not None:
        from .store import Store
        store = Store(database)
    results = []
    for path in video_files(input_path):
        if names and path.stem not in names:
            continue
        source = source_run / path.stem
        previous = json.loads((source / 'result.json').read_text(encoding='utf-8'))
        if previous.get('input_sha256') != digest(path):
            raise ValueError('Frozen recognition does not match video bytes')
        dest = output / path.stem
        dest.mkdir(parents=True, exist_ok=True)
        snapshot = store.get_inventory_snapshot(cabinet_id) if database is not None else None
        config_hash = hashlib.sha256((version_hash() + digest(source / 'result.json') +
            client.settings.model + client.settings.api_url + json.dumps(snapshot, sort_keys=True)).encode()).hexdigest()
        existing = dest / 'result.json'
        if resume and existing.exists():
            saved = json.loads(existing.read_text(encoding='utf-8'))
            if saved.get('config_hash') == config_hash and saved['status'] == 'completed':
                if database is not None:
                    store.import_run(dest, cabinet_id, base_revision=snapshot['inventory_revision'])
                results.append(saved)
                write_json(output / 'summary.json', results)
                render_report(results, output)
                print(path.name, 'resume_completed', flush=True)
                continue
        import shutil
        for folder in ('frames', 'crops', 'annotated'):
            if (source / folder).exists():
                shutil.copytree(source / folder, dest / folder, dirs_exist_ok=True)
        for filename in ('media.json', 'contact-sheet.jpg', 'evidence.json', 'tool-trace.json'):
            if (source / filename).exists():
                shutil.copy2(source / filename, dest / filename)
        result = {**previous, 'event_candidates': [], 'event_uncertainties': [], 'errors': [],
                  'config_hash': config_hash, 'recognition_source': str(source),
                  'recognition_config_hash': previous.get('config_hash'),
                  'action_version_hash': version_hash(), 'action_model': client.settings.model,
                  'action_prompt_hashes': {p.name: digest(p) for p in PROMPTS.glob('*-v3.txt')},
                  'action_analyzed_at': datetime.now(timezone.utc).isoformat(),
                  'recognition_reused': True, 'status': 'running'}
        if snapshot is not None:
            result['inventory_context'] = snapshot
        started = time.monotonic()
        try:
            analysis = analyze_operation(path, dest, client, result, structured, source=source, initial_inventory=snapshot)
            result.update(operation_analysis=analysis, event_candidates=analysis['event_candidates'],
                          event_uncertainties=analysis['pending'], status='completed')
        except (APIError, ValueError, OSError) as exc:
            result.update(status='failed', errors=[client.safe(exc)], operation_analysis={'status': 'failed'})
        result['elapsed_s'] = round(time.monotonic() - started, 2)
        write_json(dest / 'result.json', result)
        if database is not None:
            store.import_run(dest, cabinet_id, base_revision=snapshot['inventory_revision'])
        results.append(result)
        write_json(output / 'summary.json', results)
        render_report(results, output)
        print(path.name, result['status'], result['operation_analysis']['status'],
              [(e['kind'], e['candidate_ids']) for e in result['event_candidates']], flush=True)
    return results
