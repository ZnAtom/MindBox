"""Bounded GPT video slices with persistent identities and an isolated analysis ledger."""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Literal

from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.client import content, image_part, parse_json, write_json
from app.config import Settings
from app.experiments import file_hash
from app.media import inspect_media, iter_action_frames
from model_comparison.responses_client import ResponsesClient
from model_comparison.run import gpt_settings

VERSION = 'long-slices-v1'
PROMPT = Path(__file__).with_name('long_video_prompt.txt')


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class ObjectInfo(Strict):
    object_id: str = Field(pattern=r'^obj-[0-9]{3}$')
    name: str = Field(min_length=1)
    category: str
    description: str = Field(min_length=1)
    readable_text: list[str]
    identity_support: Literal['low', 'medium', 'high']


class Observation(Strict):
    object_id: str
    frame_id: str
    region: Literal['inside', 'outside', 'frame_edge', 'out_of_frame', 'occluded', 'unknown']
    bbox: list[int] | None
    location: str
    at_rest: bool
    released: bool
    drawer_phase: Literal['open', 'opening', 'closing', 'closed', 'unknown']

    @model_validator(mode='after')
    def check_box(self):
        if self.bbox is not None:
            if (len(self.bbox) != 4 or not all(0 <= x <= 1000 for x in self.bbox)
                    or self.bbox[0] >= self.bbox[2] or self.bbox[1] >= self.bbox[3]):
                raise ValueError('bbox must be normalized xyxy, 0..1000')
        if self.region in ('inside', 'outside', 'frame_edge') and self.bbox is None:
            raise ValueError('Visible objects need bbox')
        if self.region in ('out_of_frame', 'occluded') and self.bbox is not None:
            raise ValueError('Invisible objects must have bbox=null')
        return self


class Proposal(Strict):
    object_id: str
    kind: Literal['add', 'remove', 'return', 'move', 'uncertain']
    before_frame_id: str
    after_frame_id: str
    evidence_frame_ids: list[str] = Field(min_length=2)
    explanation: str
    support: Literal['low', 'medium', 'high']


class Packet(Strict):
    summary: str
    objects: list[ObjectInfo] = Field(max_length=24)
    observations: list[Observation] = Field(max_length=180)
    events: list[Proposal] = Field(max_length=30)
    uncertainties: list[str]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def plan_slices(frames, core_ms=6000, overlap_ms=2000):
    if not frames or not 2000 <= core_ms <= 12000 or not 0 <= overlap_ms <= core_ms:
        raise ValueError('Invalid slice limits or empty video')
    end = frames[-1]['media_time_ms']
    plans = []
    start = 0
    while start <= end:
        stop = min(start + core_ms, end + 1)
        context = max(0, start - overlap_ms)
        available = [f for f in frames if context <= f['media_time_ms'] < stop]
        # Local evidence is 8fps. Give GPT about 4fps plus actual endpoints.
        chosen, next_ms = [], context
        for frame in available:
            if frame['media_time_ms'] >= next_ms:
                chosen.append(frame)
                next_ms = frame['media_time_ms'] + 240
        if available and (not chosen or chosen[-1]['id'] != available[-1]['id']):
            chosen.append(available[-1])
        plans.append({'id': f'slice-{len(plans) + 1:03d}', 'core_start_ms': start,
                      'core_end_ms': stop, 'context_start_ms': context,
                      'available_frame_ids': [f['id'] for f in available],
                      'frame_ids': [f['id'] for f in chosen]})
        start = stop
    return plans


def contact_sheet(frames, root, filename='contact-sheet.jpg', interval_ms=2000):
    selected, next_ms = [], 0
    for frame in frames:
        if frame['media_time_ms'] >= next_ms:
            selected.append(frame)
            next_ms = frame['media_time_ms'] + interval_ms
    if selected[-1]['id'] != frames[-1]['id']:
        selected.append(frames[-1])
    canvas = Image.new('RGB', (4 * 384, ((len(selected) + 3) // 4) * 244), 'white')
    draw = ImageDraw.Draw(canvas)
    for i, frame in enumerate(selected):
        with Image.open(root / frame['file']) as original:
            image = original.copy()
        image.thumbnail((384, 216))
        x, y = (i % 4) * 384, (i // 4) * 244
        canvas.paste(image, (x, y + 24))
        draw.text((x + 5, y + 5), f"{frame['media_time_ms'] / 1000:.3f}s", fill='black')
    canvas.save(root / filename, quality=90)


def prepare(video, root, core_ms=6000, overlap_ms=2000):
    video, root = Path(video).resolve(), Path(root).resolve()
    config = {'version': VERSION, 'input_sha256': file_hash(video), 'fps': 8,
              'core_ms': core_ms, 'overlap_ms': overlap_ms,
              'code_sha256': file_hash(Path(__file__)), 'prompt_sha256': file_hash(PROMPT),
              'transport_sha256': file_hash(Path(__file__).with_name('responses_client.py'))}
    path = root / 'plan.json'
    if path.exists():
        plan = json.loads(path.read_text(encoding='utf-8'))
        if plan['config'] != config:
            raise ValueError('Input/config/code changed; create a new experiment directory')
        return plan
    root.mkdir(parents=True, exist_ok=True)
    frames = list(iter_action_frames(video, root, fps=8))
    plan = {'video': str(video), 'media': inspect_media(video), 'config': config,
            'frames': frames, 'slices': plan_slices(frames, core_ms, overlap_ms)}
    write_json(path, plan)
    contact_sheet(frames, root)
    for part in plan['slices']:
        selected = [f for f in frames if f['id'] in part['frame_ids']]
        contact_sheet(selected, root, part['id'] + '.jpg', interval_ms=600)
    return plan


def validate_packet(packet, delivered, history, known, part):
    new_ids = [item.object_id for item in packet.objects]
    if len(new_ids) != len(set(new_ids)):
        raise ValueError('Duplicate object IDs')
    ids = set(known) | set(new_ids)
    pairs = set()
    for point in packet.observations:
        if point.object_id not in ids or point.frame_id not in delivered:
            raise ValueError('Unknown object or observation frame not delivered to this slice')
        pair = (point.object_id, point.frame_id)
        if pair in pairs:
            raise ValueError('Duplicate object/frame observation')
        pairs.add(pair)
    evidence = {**history, **delivered}
    for event in packet.events:
        refs = set(event.evidence_frame_ids) | {event.before_frame_id, event.after_frame_id}
        if event.object_id not in ids or not refs <= set(evidence):
            raise ValueError('Event uses an unknown object or unseen evidence')
        before, after = evidence[event.before_frame_id], evidence[event.after_frame_id]
        if not before['media_time_ms'] < after['media_time_ms'] < part['core_end_ms']:
            raise ValueError('Event time is reversed or beyond the slice watermark')
    boxes = [p for p in packet.observations if p.bbox]
    from app.policy import box_iou
    for i, point in enumerate(boxes):
        for other in boxes[i + 1:]:
            if (point.frame_id == other.frame_id and point.object_id != other.object_id
                    and box_iou(point.bbox, other.bbox) > 0.75):
                raise ValueError('Competing IDs share a strongly overlapping box')


FRAME_TOOL = {'type': 'function', 'function': {'name': 'get_frames',
    'description': 'Inspect extra 8fps frames from the current/past overlap only; never future video.',
    'parameters': {'type': 'object', 'properties': {
        'start_ms': {'type': 'integer'}, 'end_ms': {'type': 'integer'},
        'reason': {'type': 'string'}}, 'required': ['start_ms', 'end_ms', 'reason'],
        'additionalProperties': False}}}


def read_slice(client, plan, part, state, root):
    lookup = {f['id']: f for f in plan['frames']}
    delivered = {fid: lookup[fid] for fid in part['frame_ids']}
    history = {p['frame_id']: lookup[p['frame_id']] for obj in state['objects'].values()
               for p in obj.get('points', [])}
    compact = {oid: {k: v for k, v in obj.items() if k not in ('points', 'prototype')}
               | {'recent_points': obj.get('points', [])[-10:]}
               for oid, obj in state['objects'].items()}
    parts = [{'type': 'text', 'text': json.dumps({'slice': part['id'],
        'core_ms': [part['core_start_ms'], part['core_end_ms']],
        'ledger': compact, 'accepted_events': state['events'][-15:],
        'schema': Packet.model_json_schema()}, ensure_ascii=False)}]
    for oid, obj in list(state['objects'].items())[-8:]:
        if obj.get('prototype'):
            parts += [{'type': 'text', 'text': 'HISTORICAL IDENTITY REFERENCE ONLY: ' + oid},
                      image_part(root / obj['prototype']['file'])]
    for frame in delivered.values():
        parts += [{'type': 'text', 'text': f"FRAME {frame['id']} TIME_MS={frame['media_time_ms']}"},
                  image_part(root / frame['file'])]
    messages = [{'role': 'system', 'content': PROMPT.read_text(encoding='utf-8')},
                {'role': 'user', 'content': parts}]
    trace = []
    tool_rounds, repairs, calls = 0, 0, 0
    while True:
        if calls >= 5:
            raise ValueError('Per-slice model call budget exhausted')
        calls += 1
        response = client.chat(messages, part['id'] + f'_r{len(trace)}', max_tokens=7500,
            json_mode=True, tools=[FRAME_TOOL] if tool_rounds < 2 else None)
        message = response['choices'][0]['message']
        if message.get('tool_calls'):
            messages.append(message)
            for call in message['tool_calls']:
                status = {'status': 'rejected', 'reason': 'invalid_or_exhausted_tool_request'}
                selected = []
                try:
                    args = json.loads(call['function']['arguments'])
                    if (call['function']['name'] == 'get_frames' and tool_rounds < 2
                            and type(args.get('start_ms')) is int and type(args.get('end_ms')) is int
                            and part['context_start_ms'] <= args['start_ms'] <= args['end_ms'] < part['core_end_ms']
                            and args['end_ms'] - args['start_ms'] <= 2500):
                        selected = [lookup[fid] for fid in part['available_frame_ids']
                                    if args['start_ms'] <= lookup[fid]['media_time_ms'] <= args['end_ms']][:20]
                        status = {'status': 'ok', 'frames': selected, 'reason': args.get('reason')}
                except (ValueError, KeyError, TypeError):
                    args = {'invalid': True}
                trace.append({'tool': call['function']['name'], 'arguments': args, 'result': status})
                write_json(root / 'slices' / part['id'] / 'tools.json', trace)
                messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                 'content': json.dumps(status, ensure_ascii=False)})
                extra = []
                for frame in selected:
                    delivered[frame['id']] = frame
                    extra += [{'type': 'text', 'text': f"FRAME {frame['id']} TIME_MS={frame['media_time_ms']}"},
                              image_part(root / frame['file'])]
                if extra:
                    messages.append({'role': 'user', 'content': extra})
                tool_rounds += 1
            continue
        raw = content(response)
        write_json(root / 'slices' / part['id'] / f'raw-{repairs}.json', {'text': raw})
        try:
            value = Packet.model_validate(parse_json(raw))
            validate_packet(value, delivered, history, state['objects'], part)
            return value.model_dump(), trace
        except ValueError as exc:
            client.log({'stage': part['id'], 'status': 'invalid_output', 'error': client.safe(exc)})
            if repairs >= 1:
                raise
            messages += [message, {'role': 'user', 'content':
                'Repair the JSON without inventing evidence. Error: ' + client.safe(exc)}]
            repairs += 1


def boundary(point):
    box = point.get('bbox')
    return bool(box and min(box[0], box[1], 1000 - box[2], 1000 - box[3]) <= 15)


def check_event(event, obj, frames):
    before = frames[event['before_frame_id']]['media_time_ms']
    after = frames[event['after_frame_id']]['media_time_ms']
    points = [p for p in obj['points'] if before <= p['media_time_ms'] <= after]
    if event['support'] == 'low' or obj.get('identity_support') == 'low':
        return 'physical_identity_or_action_uncertain'
    if event['kind'] == 'uncertain':
        return 'model_uncertain'
    edges = [p for p in points if p['region'] in ('frame_edge', 'outside') and boundary(p)]
    rests = [p for p in points if p['region'] == 'inside' and p['at_rest'] and p['released']
             and p['drawer_phase'] == 'open']
    if event['kind'] in ('add', 'return'):
        if obj['presence'] == 'inside':
            return 'already_inside_or_overlap_duplicate'
        for edge in edges:
            following = [p for p in rests if p['media_time_ms'] > edge['media_time_ms']]
            if len(following) >= 2 and following[-1]['media_time_ms'] - following[0]['media_time_ms'] >= 180:
                return None
        return 'entry_boundary_or_released_placement_missing'
    if event['kind'] == 'remove':
        if obj['presence'] != 'inside':
            return 'prior_inside_state_missing'
        inside = [p for p in points if p['region'] == 'inside']
        gone = [p for p in points if p['region'] == 'out_of_frame' and p['drawer_phase'] == 'open']
        if any(a['media_time_ms'] < b['media_time_ms'] < c['media_time_ms']
               for a in inside for b in edges for c in gone):
            return None
        return 'drawer_to_video_edge_to_exit_not_observed'
    if event['kind'] == 'move':
        if obj['presence'] != 'inside':
            return 'prior_inside_state_missing'
        locations = {p['location'] for p in rests if p['location'] not in ('unknown', '')}
        if len(locations) >= 2:
            return None
        return 'two_distinct_drawer_relative_rest_locations_missing'
    return 'unsupported_event'


def apply_packet(state, packet, lookup, part):
    # Replay from immutable slice outputs makes database writes idempotent on resume.
    for item in packet['objects']:
        entry = state['objects'].setdefault(item['object_id'], {'points': [], 'presence': 'unknown',
            'ever_inside': False, 'location': 'unknown', 'metadata_history': []})
        entry['metadata_history'].append({'slice_id': part['id'], **item})
        entry.update(item)
    touched = set()
    for point in sorted(packet['observations'], key=lambda p: lookup[p['frame_id']]['media_time_ms']):
        obj = state['objects'][point['object_id']]
        old = next((p for p in obj['points'] if p['frame_id'] == point['frame_id']), None)
        if old:
            keys = ('region', 'at_rest', 'released', 'location')
            if any(old[k] != point[k] for k in keys):
                state['conflicts'].append({'slice_id': part['id'], 'reason': 'overlap_observation_disagrees',
                    'object_id': point['object_id'], 'old': old, 'new': point})
            continue
        obj['points'].append({**point, 'media_time_ms': lookup[point['frame_id']]['media_time_ms']})
        touched.add(point['object_id'])
    for obj in state['objects'].values():
        obj['points'].sort(key=lambda p: p['media_time_ms'])
    proposals = sorted(packet['events'], key=lambda e: lookup[e['after_frame_id']]['media_time_ms'])
    # A visible pre-existing object can establish presence, but never fabricates an insertion.
    for oid in sorted(touched):
        obj = state['objects'][oid]
        events = [e for e in proposals if e['object_id'] == oid and e['kind'] in ('add', 'return')]
        limit = min((lookup[e['before_frame_id']]['media_time_ms'] for e in events), default=part['core_end_ms'])
        candidates = [p for p in obj['points'] if p['region'] == 'inside' and p['at_rest'] and p['released']
                      and p['drawer_phase'] == 'open' and p['media_time_ms'] <= limit]
        incoming = candidates and any(boundary(p) and p['region'] in ('outside', 'frame_edge')
            and p['media_time_ms'] < candidates[0]['media_time_ms'] for p in obj['points'])
        if (obj['presence'] == 'unknown' and candidates and not incoming
                and obj.get('identity_support') != 'low'):
            obj.update(presence='inside', ever_inside=True, location=candidates[0]['location'])
            state['baselines'].append({'object_id': oid, 'frame_id': candidates[0]['frame_id'],
                'media_time_ms': candidates[0]['media_time_ms'], 'kind': 'observed_existing',
                'filmed_inventory_action': False})
    for proposal in proposals:
        obj = state['objects'][proposal['object_id']]
        event = {**proposal, 'slice_id': part['id'], 'event_window_ms':
                 [lookup[proposal[k]]['media_time_ms'] for k in ('before_frame_id', 'after_frame_id')]}
        same = [old for old in state['events'] if old['object_id'] == event['object_id']
                and old['kind'] in (('add', 'return') if event['kind'] in ('add', 'return') else (event['kind'],))
                and max(old['event_window_ms'][0], event['event_window_ms'][0])
                    <= min(old['event_window_ms'][1], event['event_window_ms'][1])]
        conflict = any(c['object_id'] == event['object_id']
            and event['event_window_ms'][0] <= c['old']['media_time_ms'] <= event['event_window_ms'][1]
            and c['old']['region'] != c['new']['region'] for c in state['conflicts'])
        reason = ('overlap_duplicate' if same else 'conflicting_region_evidence' if conflict
                  else check_event(event, obj, lookup))
        event.update(status='accepted' if reason is None else 'pending', reason=reason)
        event['event_id'] = 'evt-' + digest([event['object_id'], event['kind'], event['event_window_ms']])[:20]
        state['proposals'].append(event)
        if reason is not None:
            continue
        if event['kind'] in ('add', 'return'):
            event['kind'] = 'return' if obj['ever_inside'] else 'add'
            obj.update(presence='inside', ever_inside=True)
        elif event['kind'] == 'remove':
            obj['presence'] = 'outside'
        inside = [p for p in obj['points'] if p['region'] == 'inside' and p['released']
                  and p['media_time_ms'] <= event['event_window_ms'][1]]
        if inside:
            obj['location'] = inside[-1]['location']
        state['events'].append(event)
    for obj in state['objects'].values():
        if obj['points']:
            obj['last_observed'] = obj['points'][-1]
    state['summaries'].append({'slice_id': part['id'], 'summary': packet['summary'],
                               'uncertainties': packet['uncertainties']})


def prototypes(state, lookup, root):
    for oid, obj in state['objects'].items():
        points = [p for p in obj['points'] if p.get('bbox')]
        if not points:
            continue
        def rank(p):
            return (p['released'] and p['at_rest'], p['region'] == 'inside',
                    lookup[p['frame_id']]['sharpness'])
        point = max(points, key=rank)
        frame = lookup[point['frame_id']]
        with Image.open(root / frame['file']) as image:
            x1, y1, x2, y2 = point['bbox']
            crop = image.crop((int(x1 * image.width / 1000), int(y1 * image.height / 1000),
                               int(x2 * image.width / 1000), int(y2 * image.height / 1000)))
            path = f'crops/{oid}-{point["frame_id"]}.jpg'
            (root / 'crops').mkdir(exist_ok=True)
            crop.save(root / path, quality=94)
        obj['prototype'] = {'file': path, 'frame_id': point['frame_id'],
                            'media_time_ms': frame['media_time_ms'], 'bbox': point['bbox']}


def save_database(root, state, plan, status):
    with sqlite3.connect(root / 'analysis.sqlite3') as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS session (id TEXT PRIMARY KEY, status TEXT, data_json TEXT);
            CREATE TABLE IF NOT EXISTS objects (id TEXT PRIMARY KEY, name TEXT, presence TEXT, data_json TEXT);
            CREATE TABLE IF NOT EXISTS observations (object_id TEXT REFERENCES objects(id), frame_id TEXT,
                media_time_ms INTEGER, data_json TEXT, PRIMARY KEY(object_id, frame_id));
            CREATE TABLE IF NOT EXISTS proposals (sequence INTEGER PRIMARY KEY, event_id TEXT, status TEXT, data_json TEXT);
            CREATE TABLE IF NOT EXISTS accepted_events (id TEXT PRIMARY KEY, object_id TEXT REFERENCES objects(id),
                kind TEXT, start_ms INTEGER, end_ms INTEGER, data_json TEXT);
        ''')
        encode = lambda value: json.dumps(value, ensure_ascii=False)
        db.execute('DELETE FROM accepted_events')
        db.execute('DELETE FROM proposals')
        db.execute('DELETE FROM observations')
        db.execute('DELETE FROM objects')
        for oid, obj in state['objects'].items():
            db.execute('INSERT INTO objects VALUES (?,?,?,?)', (oid, obj['name'], obj['presence'], encode(obj)))
            for point in obj['points']:
                db.execute('INSERT INTO observations VALUES (?,?,?,?)',
                           (oid, point['frame_id'], point['media_time_ms'], encode(point)))
        for i, event in enumerate(state['proposals']):
            db.execute('INSERT INTO proposals VALUES (?,?,?,?)', (i, event['event_id'], event['status'], encode(event)))
        for event in state['events']:
            db.execute('INSERT INTO accepted_events VALUES (?,?,?,?,?,?)', (event['event_id'], event['object_id'],
                event['kind'], *event['event_window_ms'], encode(event)))
        db.execute('INSERT OR REPLACE INTO session VALUES (?,?,?)',
                   (plan['config']['input_sha256'], status, encode(plan['config'])))
        counts = {name: db.execute('SELECT COUNT(*) FROM ' + name).fetchone()[0]
                  for name in ('objects', 'observations', 'proposals', 'accepted_events')}
    return {'database': str(root / 'analysis.sqlite3'), 'kind': 'isolated_analysis_ledger',
            'production_inventory_modified': False, 'counts': counts}


def report(root, state, plan, status):
    receipt = save_database(root, state, plan, status)
    write_json(root / 'result.json', {'status': status, 'model': state['model'], **state,
        'database_receipt': receipt, 'reference_answer': None, 'accuracy_measured': False,
        'audio_analyzed': False, 'stored_at': None,
        'limits': ['No human answer labels', 'Model observations are not ground truth',
                   'Analysis ledger is separate from production inventory',
                   'Offline bounded replay; no wall-clock realtime claim']})
    labels = {'add': '存入', 'remove': '取出', 'return': '放回', 'move': '移动', 'uncertain': '不确定'}
    lines = ['# v11 分片分析', '', f'状态：{status}；模型：{state["model"]}。无人工答案，不能据此计算准确率。', '',
             '## 数据库回执', '', '```json', json.dumps(receipt, ensure_ascii=False, indent=2), '```', '',
             '## 物品台账', '', '| ID | 名称 | 外观 | 台账位置 | 证据 |', '| --- | --- | --- | --- | --- |']
    for oid, obj in state['objects'].items():
        evidence = obj.get('prototype', {})
        link = f'[{evidence.get("media_time_ms", 0)/1000:.2f}s]({evidence["file"]})' if evidence else '无'
        lines.append(f'| {oid} | {obj["name"]} | {obj["description"].replace("|", "/")} | '
                     f'{obj["presence"]} / {obj["location"]} | {link} |')
    lines += ['', '## 符合规则的动作', '', '| 时间 | 物品 | 动作 | 解释 |', '| --- | --- | --- | --- |']
    for e in state['events']:
        a, b = e['event_window_ms']
        lines.append(f'| {a/1000:.2f}–{b/1000:.2f}s | {e["object_id"]} | {labels[e["kind"]]} | {e["explanation"].replace("|", "/")} |')
    lines += ['', '## 分段理解', '']
    for item in state['summaries']:
        lines += [f'### {item["slice_id"]}', '', item['summary'], '']
        lines += ['- ' + u for u in item['uncertainties']]
        lines += ['']
    lines += ['## 待确认与限制', '', '以下为模型提案，未自动改变台账中的库存状态。', '']
    for e in state['proposals']:
        if e['status'] == 'pending':
            lines += [f'- {e["event_window_ms"]}ms {e["object_id"]} {e["kind"]}：{e["reason"]}；{e["explanation"]}']
    lines += ['', f'重叠观察冲突：{len(state["conflicts"])} 条，详见 result.json。', '',
              '本轮只分析画面；音轨尚未用于物品或日程。媒体时间不是实际放入日期。', '',
              '## 完整流程', '', '```mermaid', 'flowchart TD',
              '  A[只读原视频] --> B[原始PTS解码 8fps本地证据]',
              '  B --> C[6秒核心片段 加2秒历史重叠]',
              '  C --> D[约4fps画面 加全局台账与历史外观]',
              '  D --> E[GPT识别实物 位置与动作提案]',
              '  E --> F{证据是否足够}',
              '  F -->|需要补帧| G[get_frames 当前片段内8fps 最多两轮]',
              '  G --> E', '  F -->|已有结果| H[程序校验引用 身份 进出边界 放稳]',
              '  H --> I[合并重叠事件 保留跨段身份]',
              '  I --> J[隔离SQLite 物品 观察 事件与待确认]',
              '  J --> K[更新台账 交给下一片段]',
              '  K --> C', '  J --> L[时间线 截图 回执与最终台账报告]', '```', '']
    (root / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    write_json(root / 'database-receipt.json', receipt)
    return receipt


def run(video, root, *, prepare_only=False, max_slices=20, model='gpt-6.1-sol'):
    root = Path(root).resolve()
    plan = prepare(video, root)
    if prepare_only:
        return {'status': 'prepared', 'slices': len(plan['slices']), 'frames': len(plan['frames'])}
    Settings.load()
    client = ResponsesClient(gpt_settings(model), root / 'api')
    lookup = {f['id']: f for f in plan['frames']}
    state = {'model': model, 'objects': {}, 'events': [], 'proposals': [],
             'baselines': [], 'conflicts': [], 'summaries': []}
    status = 'completed'
    for i, part in enumerate(plan['slices']):
        if i >= max_slices:
            status = 'budget_paused'
            break
        directory = root / 'slices' / part['id']
        destination = directory / 'packet.json'
        before_hash = digest(state)
        try:
            if destination.exists():
                record = json.loads(destination.read_text(encoding='utf-8'))
                if record['state_before_sha256'] != before_hash or record['model'] != model:
                    raise ValueError('Slice resume state/model mismatch')
                packet = Packet.model_validate(record['packet']).model_dump()
            else:
                packet, trace = read_slice(client, plan, part, state, root)
                write_json(destination, {'state_before_sha256': before_hash, 'model': model,
                                         'packet': packet, 'tools': trace})
            candidate_state = copy.deepcopy(state)
            apply_packet(candidate_state, packet, lookup, part)
            prototypes(candidate_state, lookup, root)
            receipt = report(root, candidate_state, plan, 'processing')
            state = candidate_state
            print(json.dumps({'slice': part['id'], 'objects': len(state['objects']),
                              'events': len(state['events']), 'database': receipt['counts']}, ensure_ascii=False), flush=True)
        except Exception as exc:
            status = 'paused_error'
            write_json(directory / 'failure.json', {'error': client.safe(exc), 'type': type(exc).__name__,
                'at': datetime.now(timezone.utc).isoformat()})
            report(root, state, plan, status)
            raise
    receipt = report(root, state, plan, status)
    return {'status': status, **receipt}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--max-slices', type=int, default=20)
    parser.add_argument('--model', default='gpt-6.1-sol')
    args = parser.parse_args()
    print(json.dumps(run(args.video, args.root, prepare_only=args.prepare_only,
                         max_slices=args.max_slices, model=args.model), ensure_ascii=False))


if __name__ == '__main__':
    main()
