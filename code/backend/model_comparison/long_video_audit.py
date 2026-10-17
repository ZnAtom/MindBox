"""Read-only ledger audit and evidence report; never calls models or reads answers."""
import argparse
from collections import Counter
import copy
from fractions import Fraction
import json
from pathlib import Path
import sqlite3

import av
from PIL import Image, ImageDraw, ImageFont

from app.client import write_json


def unresolved(proposals, accepted):
    pending, resolved, duplicates = [], [], []
    for event in proposals:
        if event['status'] != 'pending':
            continue
        if event['reason'] == 'overlap_duplicate':
            duplicates.append(event)
            continue
        family = ('add', 'return') if event['kind'] in ('add', 'return') else (event['kind'],)
        a, b = event['event_window_ms']
        matches = [e for e in accepted if e['object_id'] == event['object_id'] and e['kind'] in family
                   and max(a, e['event_window_ms'][0]) <= min(b, e['event_window_ms'][1])
                   and e['event_window_ms'][1] >= b]
        if matches:
            resolved.append({'proposal': event, 'resolved_by': matches[0]['event_id']})
        else:
            pending.append(event)
    return pending, resolved, duplicates


def export_clip(part, frames, root):
    destination = root / 'clips' / (part['id'] + '.mp4')
    if destination.exists():
        return destination.relative_to(root).as_posix()
    destination.parent.mkdir(exist_ok=True)
    selected = [frames[fid] for fid in part['available_frame_ids']]
    with Image.open(root / selected[0]['file']) as first:
        width, height = first.size
    with av.open(str(destination), 'w') as container:
        stream = container.add_stream('libx264', rate=8)
        stream.width, stream.height, stream.pix_fmt = width, height, 'yuv420p'
        stream.time_base = Fraction(1, 1000)
        stream.options = {'crf': '25', 'preset': 'veryfast'}
        for item in selected:
            with Image.open(root / item['file']) as source:
                frame = av.VideoFrame.from_image(source.convert('RGB'))
            frame.pts = item['media_time_ms'] - selected[0]['media_time_ms']
            frame.time_base = Fraction(1, 1000)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return destination.relative_to(root).as_posix()


def evidence_grid(items, root):
    items = [(oid, obj) for oid, obj in items if obj.get('prototype')]
    if not items:
        return None
    canvas = Image.new('RGB', (3 * 420, ((len(items) + 2) // 3) * 300), 'white')
    draw = ImageDraw.Draw(canvas)
    font_path = Path('C:/Windows/Fonts/msyh.ttc')
    font = ImageFont.truetype(str(font_path), 19) if font_path.exists() else ImageFont.load_default()
    for i, (oid, obj) in enumerate(items):
        x, y = (i % 3) * 420, (i // 3) * 300
        ref = obj['prototype']
        with Image.open(root / ref['file']) as source:
            image = source.copy()
        image.thumbnail((400, 235))
        canvas.paste(image, (x + (420 - image.width) // 2, y + 52))
        draw.text((x + 8, y + 3), oid + ' ' + obj['name'][:17], fill='black', font=font)
        draw.text((x + 8, y + 28), f"{ref['media_time_ms'] / 1000:.2f}s", fill='black', font=font)
    canvas.save(root / 'item-evidence.jpg', quality=92)
    return 'item-evidence.jpg'


def catalog_views(items, events, frames, root):
    """Use first released placement before later stacking can obscure the product."""
    result = []
    (root / 'catalog-crops').mkdir(exist_ok=True)
    for oid, original in items:
        obj = copy.deepcopy(original)
        placement = next((e for e in events if e['object_id'] == oid and e['kind'] in ('add', 'return')), None)
        point = next((p for p in obj['points'] if placement and p['frame_id'] == placement['after_frame_id']
                      and p.get('bbox')), None)
        if point:
            source = frames[point['frame_id']]
            path = f'catalog-crops/{oid}.jpg'
            with Image.open(root / source['file']) as image:
                x1, y1, x2, y2 = point['bbox']
                crop = image.crop((int(x1 * image.width / 1000), int(y1 * image.height / 1000),
                                   int(x2 * image.width / 1000), int(y2 * image.height / 1000)))
                crop.save(root / path, quality=95)
            obj['prototype'] = {'file': path, 'frame_id': point['frame_id'],
                'media_time_ms': source['media_time_ms'], 'bbox': point['bbox'],
                'selection': 'first_accepted_released_placement_for_catalog_only'}
        result.append((oid, obj))
    return result


def audit(root, clips=False):
    root = Path(root).resolve()
    state = json.loads((root / 'result.json').read_text(encoding='utf-8'))
    plan = json.loads((root / 'plan.json').read_text(encoding='utf-8'))
    lookup = {f['id']: f for f in plan['frames']}
    focus = [(oid, obj) for oid, obj in state['objects'].items()
             if any(p['region'] == 'inside' for p in obj['points'])
             or any(e['object_id'] == oid for e in state['proposals'])]
    focus = catalog_views(focus, state['events'], lookup, root)
    pending, resolved, duplicates = unresolved(state['proposals'], state['events'])
    with sqlite3.connect((root / 'analysis.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
        counts = {table: db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                  for table in ('objects', 'observations', 'proposals', 'accepted_events')}
    if counts != state['database_receipt']['counts'] or integrity != 'ok':
        raise ValueError('Database receipt/integrity mismatch')
    entries = [json.loads(line) for line in (root / 'api/api-attempts.jsonl').read_text(encoding='utf-8').splitlines()]
    success = [e for e in entries if e['status'] == 'success']
    api = {'successes': len(success), 'failures': [e for e in entries if e['status'] == 'failed'],
           'invalid_outputs': [e for e in entries if e['status'] == 'invalid_output'],
           'actual_model_ids': sorted({e['actual_model'] for e in success}),
           'input_tokens': sum(e.get('usage', {}).get('prompt_tokens', 0) for e in success),
           'output_tokens': sum(e.get('usage', {}).get('completion_tokens', 0) for e in success),
           'http_elapsed_s': round(sum(e['elapsed_s'] for e in success), 2)}
    tools = [entry for path in sorted((root / 'slices').glob('*/tools.json'))
             for entry in json.loads(path.read_text(encoding='utf-8'))]
    projections = []
    for oid, obj in focus:
        visible = [p for p in obj['points'] if p['region'] == 'inside' and p['released'] and p['at_rest']]
        latest = visible[-1] if visible else None
        projections.append({'object_id': oid, 'name': obj['name'], 'ledger_presence': obj['presence'],
            'ledger_location': obj['location'], 'last_observed': obj.get('last_observed'),
            'last_visible_rest': latest, 'needs_location_review': bool(latest and obj['presence'] == 'inside'
                and latest['location'] != obj['location'])})
    result = {'status': state['status'], 'database_integrity': integrity, 'database_counts': counts,
        'focus_object_ids': [oid for oid, _ in focus], 'background_object_count': len(state['objects']) - len(focus),
        'catalog_evidence': {oid: obj.get('prototype') for oid, obj in focus},
        'accepted_event_counts': dict(Counter(e['kind'] for e in state['events'])),
        'unresolved_proposals': pending, 'resolved_proposals': resolved, 'overlap_duplicates': duplicates,
        'projection_review': projections, 'overlap_conflicts': state['conflicts'], 'api': api,
        'tools': {'calls': len(tools), 'accepted': sum(e['result']['status'] == 'ok' for e in tools)},
        'accuracy_measured': False, 'production_inventory_modified': False}
    media = {}
    if clips:
        for part in plan['slices']:
            media[part['id']] = export_clip(part, lookup, root)
    image = evidence_grid(focus, root)
    write_json(root / 'audit.json', result)
    labels = {'add': '存入', 'remove': '取出', 'return': '放回', 'move': '移动', 'uncertain': '不确定'}
    reason_labels = {'entry_boundary_or_released_placement_missing': '缺视频边缘进入或松手放稳证据',
        'drawer_to_video_edge_to_exit_not_observed': '缺抽屉到视频边缘再完全离开的路径',
        'prior_inside_state_missing': '台账尚未确认原先在抽屉内',
        'two_distinct_drawer_relative_rest_locations_missing': '缺两个不同抽屉相对位置的放稳证据',
        'conflicting_region_evidence': '重叠片段对同帧区域判断冲突',
        'physical_identity_or_action_uncertain': '实物关联或动作支持不足',
        'already_inside_or_overlap_duplicate': '已在抽屉内或重复事件', 'model_uncertain': '模型无法确认'}
    lines = ['# v11 连续流程分析', '',
        f'使用 `{state["model"]}`。状态：`{state["status"]}`。没有人工答案，下列是模型观察与程序规则校验结果，不能视作已验证准确率。', '',
        f'抽屉/操作相关物品 **{len(focus)} 件**，另外识别到背景物品 {result["background_object_count"]} 件；背景物品不算入柜。', '',
        f'符合规则的动作 {len(state["events"])} 条；尚待确认 {len(pending)} 条；早期待确认后续补齐 {len(resolved)} 条。', '',
        '## 物品与位置', '']
    if image:
        lines += [f'![物品证据]({image})', '']
    lines += ['| 实物ID | 名称 | 类别与外观 | 动作台账位置 | 最后可见放稳位置 |', '| --- | --- | --- | --- | --- |']
    for (oid, obj), projection in zip(focus, projections):
        latest = projection['last_visible_rest']
        last = f'{latest["location"]}，{latest["media_time_ms"]/1000:.2f}s' if latest else '未观察到'
        lines.append(f'| {oid} | {obj["name"]} | {obj["category"]}；{obj["description"].replace("|", "/")} | '
                     f'{obj["presence"]} / {obj["location"]} | {last} |')
    lines += ['', '位置来自模型对抽屉的相对位置判断；抽屉编号未人工校准。闭合后不可见不自动扣除库存。媒体时间不是实际日期。', '',
              '## 动作时间线', '', '| 时间 | 实物 | 动作 | 观察依据 |', '| --- | --- | --- | --- |']
    for e in state['events']:
        a, b = e['event_window_ms']
        oid = e['object_id']
        evidence = f'[{a/1000:.2f}]({lookup[e["before_frame_id"]]["file"]})–[{b/1000:.2f}s]({lookup[e["after_frame_id"]]["file"]})'
        lines.append(f'| {evidence} | {oid} {state["objects"][oid]["name"]} | {labels[e["kind"]]} | {e["explanation"].replace("|", "/")} |')
    lines += ['', '## 尚待确认', '']
    if not pending:
        lines += ['没有未解决的动作提案；这不等于已证明没有漏检。', '']
    for e in pending:
        a, b = e['event_window_ms']
        lines += [f'- {a/1000:.2f}–{b/1000:.2f}s，{e["object_id"]} {labels[e["kind"]]}：'
                  f'{reason_labels.get(e["reason"],e["reason"])}。{e["explanation"]}']
    if resolved:
        lines += ['', '已被后续片段补齐的旧提案只保留历史，不再算当前失败：', '']
        lines += [f'- {e["proposal"]["object_id"]}：{e["proposal"]["event_window_ms"]} → {e["resolved_by"]}' for e in resolved]
    for item in projections:
        if item['needs_location_review']:
            lines += [f'- 位置待核对：{item["object_id"]}台账为{item["ledger_location"]}，最后放稳观察为'
                      f'{item["last_visible_rest"]["location"]}；保留观察，不强行覆盖。']
    lines += ['', f'同帧重叠观察冲突 {len(state["conflicts"])} 条，完整旧/新声明见 `audit.json`。', '',
              '## 每段发生了什么', '', '| 核心时间 | 实际供图范围 | 分段理解 |', '| --- | --- | --- |']
    for part, summary in zip(plan['slices'], state['summaries']):
        selected = [lookup[fid] for fid in part['available_frame_ids']]
        link = f'[{part["id"]}]({media[part["id"]]})' if media else part['id']
        lines.append(f'| {part["core_start_ms"]/1000:.2f}–{part["core_end_ms"]/1000:.2f}s | '
            f'{link}，{selected[0]["media_time_ms"]/1000:.2f}–{selected[-1]["media_time_ms"]/1000:.2f}s | {summary["summary"].replace("|", "/")} |')
    lines += ['', '切片MP4为本地抽样帧生成的无声预览；模型实际接收逐张原图，原始音视频不改动。', '',
              '## 数据库实际回执', '', f'数据库：`{state["database_receipt"]["database"]}`', '',
              '```json', json.dumps({'integrity': integrity, **counts, 'production_inventory_modified': False}, ensure_ascii=False, indent=2), '```', '',
              '这是独立分析库，已保存物品信息、时序观察、动作提案和规则通过的事件；没有调用业务Store事务，没有改变正式库存。', '',
              '## 调用、失败与限制', '',
              f'- HTTP成功{api["successes"]}次，失败{len(api["failures"])}次，输出结构/引用校验失败{len(api["invalid_outputs"])}次。',
              f'- 接口返回模型ID：{api["actual_model_ids"]}。中转站底层模型身份未独立验证。',
              f'- get_frames实际调用{len(tools)}次，其中有效{result["tools"]["accepted"]}次；只能访问该片段已到达帧。',
              f'- 输入/输出tokens：{api["input_tokens"]}/{api["output_tokens"]}；成功HTTP耗时累计{api["http_elapsed_s"]}秒，不等于实时处理。',
              '- 没有读ans.md或人工补造动作；未知品名、漏检和错误关联仍可能存在。',
              '- 尚未接入商品RAG、网络搜索、提醒或正式设备流。', '']
    for e in api['failures']:
        lines += [f'- 失败记录：{e["stage"]} attempt={e["attempt"]}，{e.get("error_type")}，{e.get("error")}']
    lines += ['', '## 完整流程框图', '', '```mermaid', 'flowchart TD',
        '  A[原视频只读归档与哈希] --> B[PTS解码 8fps证据帧]',
        '  B --> C[6秒新片段 加2秒历史重叠]',
        '  C --> D[约4fps原图 加全局实物ID 位置与历史外观]',
        '  D --> E[GPT 6.1-sol 认物与时序观察]',
        '  E --> F{需要补看证据}', '  F -->|是| G[get_frames 当前区间8fps 有界调用]',
        '  G --> E', '  F -->|否| H[动作提案和真实帧引用校验]',
        '  H --> I[进出视频边界 松手放稳 抽屉遮挡保护]',
        '  I --> J[跨段续接 去重 移动不增数量]',
        '  J --> K[隔离SQLite 实物 观察 接受事件 待确认]',
        '  K --> C', '  K --> L[只读复核 物品目录 时间线 截图片段 回执]',
        '  L -.-> M[待接 业务库存事务 RAG 日程 设备流]', '```', '']
    (root / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    return {k: result[k] for k in ('status', 'database_integrity', 'database_counts', 'focus_object_ids', 'accepted_event_counts')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--clips', action='store_true')
    args = parser.parse_args()
    print(json.dumps(audit(args.root, args.clips), ensure_ascii=False))
