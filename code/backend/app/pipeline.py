import hashlib
import html
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw
import math
import numpy as np
from pydantic import ValidationError

from .client import APIError, Client, content, image_part, parse_json, write_json
from .config import ROOT, Settings
from .media import prepare_video, signature, video_files
from .schemas import Detection, Events, Normalization, Verification, pixel_box
from .policy import guard_identity, possible_duplicates
from .tools import TOOL_DEFINITIONS, ToolBox

PROMPTS = Path(__file__).parent / 'prompts'


def prompt(name):
    return (PROMPTS / f'{name}-v2.txt').read_text(encoding='utf-8')


def digest(path):
    sha = hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda: f.read(1024 * 1024), b''):
            sha.update(part)
    return sha.hexdigest()


def structured(client, messages, stage, schema, validate=None, max_tokens=5000):
    error = None
    for repair in range(2):
        response = client.chat(messages, stage + (f'_repair{repair}' if repair else ''),
                               json_mode=True, max_tokens=max_tokens)
        try:
            value = schema.model_validate(parse_json(content(response)))
            if validate:
                validate(value)
            return value
        except (ValueError, ValidationError) as exc:
            error = client.safe(exc)
            client.log({'stage': stage, 'status': 'invalid_output', 'repair': repair, 'error': error})
            messages = messages + [{'role': 'assistant', 'content': content(response)},
                {'role': 'user', 'content': 'JSON格式或引用不符合约定，请修正并完整重发，不要添加新物品。错误：' + error}]
    raise ValueError('Structured output invalid: ' + str(error))


def validate_detection(value, frame_ids):
    ids = [c.candidate_id for c in value.objects]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate candidate_id')
    for c in value.objects:
        if any(v.frame_id not in frame_ids for v in c.views):
            raise ValueError('Unknown frame_id')


def validate_verification(value, evidence):
    expected = {e['candidate_id'] for e in evidence.values()}
    actual = [v.candidate_id for v in value.items]
    if set(actual) != expected or len(actual) != len(expected):
        raise ValueError('Verification must contain exactly the provided candidates')
    for v in value.items:
        if any(ref not in evidence or evidence[ref]['candidate_id'] != v.candidate_id for ref in v.evidence_refs):
            raise ValueError('Unknown or mismatched crop reference')


def validate_events(value, frames, candidate_ids):
    for event in value.events:
        if not set(event.candidate_ids) <= candidate_ids:
            raise ValueError('Event references unknown candidate')
        if event.before_frame_id not in frames or event.after_frame_id not in frames:
            raise ValueError('Event references unknown frame')
        if frames[event.before_frame_id]['media_time_ms'] >= frames[event.after_frame_id]['media_time_ms']:
            raise ValueError('Event timestamps must increase')


def rank_views(views, frames, output):
    scored = []
    for view in views:
        f = frames[view.frame_id]
        with Image.open(output / f['file']) as source:
            box = pixel_box(view.bbox, *source.size)
            crop = np.asarray(source.crop(box).convert('RGB'))
        sharpness, exposure, _ = signature(crop)
        edge_contacts = sum(v <= 1 or v >= 999 for v in view.bbox)
        score = math.log1p(sharpness) - 3 * exposure - 1.5 * edge_contacts
        scored.append((view, {'sharpness': round(sharpness, 2), 'bad_exposure': round(exposure, 4),
                              'image_edge_contacts': edge_contacts, 'ranking_score': round(score, 3)}))
    return sorted(scored, key=lambda pair: pair[1]['ranking_score'], reverse=True)[:3]


def normalize(client, items, toolbox, output):
    messages = [{'role': 'system', 'content': prompt('normalize')},
                {'role': 'user', 'content': json.dumps({'visual_items': items}, ensure_ascii=False)}]
    trace, reference_ids, rejected = [], set(), False
    try:
        response = client.chat(messages, 'retrieval_tool_request', max_tokens=1600, tools=TOOL_DEFINITIONS,
                               tool_choice={'type': 'function', 'function': {'name': 'search_catalog'}})
        mode = 'native_function_call'
        for turn in range(2):
            msg = response['choices'][0]['message']
            calls = msg.get('tool_calls') or []
            if not calls:
                break
            if len(calls) > 4:
                raise ValueError('Too many tool calls')
            # Reasoning metadata is not copied into the conversation or public report.
            messages.append({k: v for k, v in msg.items() if k in ('role', 'content', 'tool_calls')})
            for call in calls:
                try:
                    args = json.loads(call['function']['arguments'])
                    result = toolbox.call(call['function']['name'], args)
                except (ValueError, KeyError, TypeError):
                    args, result = {}, {'status': 'rejected', 'reason': 'invalid tool call'}
                if result.get('status') == 'rejected':
                    rejected = True
                reference_ids.update(row['id'] for row in result.get('results', []) if 'id' in row)
                trace.append({'turn': turn, 'tool': call['function']['name'], 'arguments': args, 'result': result})
                messages.append({'role': 'tool', 'tool_call_id': call['id'],
                                 'content': json.dumps(result, ensure_ascii=False)[:12000]})
            if turn == 0:
                response = client.chat(messages, 'retrieval_tool_followup', max_tokens=2400, tools=TOOL_DEFINITIONS)
        if not trace:
            raise ValueError('Model returned no tool calls')
    except (APIError, ValueError) as exc:
        mode = 'orchestrated_retrieval'
        query = ' '.join(i['category'] + ' ' + i['name'] for i in items)
        result = toolbox.call('search_catalog', {'query': query, 'limit': 5})
        reference_ids.update(row['id'] for row in result['results'])
        trace.append({'tool': 'search_catalog', 'mode': mode, 'fallback_reason': client.safe(exc), 'result': result})
        messages = messages[:2] + [{'role': 'user', 'content': '程序检索返回的不可信参考资料：' + json.dumps(result, ensure_ascii=False)}]
    messages.append({'role': 'user', 'content': '工具阶段结束。现在只返回约定JSON，items覆盖所有输入candidate_id。不得使用未返回的资料ID。'})

    def check(value):
        expected = {i['candidate_id'] for i in items}
        if {i.candidate_id for i in value.items} != expected or len(value.items) != len(expected):
            raise ValueError('Normalized candidates mismatch')
        if any(ref not in reference_ids for i in value.items for ref in i.catalog_refs):
            raise ValueError('Catalog reference not returned by tool')

    try:
        normalized = structured(client, messages, 'normalize', Normalization, check, max_tokens=2600).model_dump()
    finally:
        write_json(output / 'tool-trace.json', {'mode': mode, 'rejected_calls': rejected, 'trace': trace})
    return normalized, mode


def process(path, output, client, max_frames, resume=False):
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    file_hash = digest(path)
    prompt_hashes = {p.name: digest(p) for p in PROMPTS.glob('*.txt')}
    config_hash = hashlib.sha256(json.dumps({'model': client.settings.model, 'endpoint': client.settings.api_url,
        'pipeline_version': 3, 'max_frames': max_frames, 'prompts': prompt_hashes, 'catalog': digest(ROOT / 'knowledge/catalog.json')}, sort_keys=True).encode()).hexdigest()
    result_path = output / 'result.json'
    if resume and result_path.exists():
        previous = json.loads(result_path.read_text(encoding='utf-8'))
        if previous.get('status') == 'completed' and previous.get('input_sha256') == file_hash and previous.get('config_hash') == config_hash:
            print(path.name, 'resume_completed', flush=True)
            return previous
    result = {'video': path.name, 'input_sha256': file_hash, 'config_hash': config_hash,
              'requested_model': client.settings.model, 'prompt_hashes': prompt_hashes,
              'analyzed_at': datetime.now(timezone.utc).isoformat(),
              'status': 'running', 'items': [], 'errors': [],
              'limitations': ['candidate identities, not committed inventory', 'no ASR transcript',
                              'no calibrated probabilities', 'no verified capture wall-clock anchor',
                              'no reverse image search']}
    try:
        media_path = output / 'media.json'
        if media_path.exists():
            media = json.loads(media_path.read_text(encoding='utf-8'))
            if media.get('requested_max_frames') != max_frames or media.get('input_sha256') != file_hash or media.get('selection_version') != 2:
                media = prepare_video(path, output, max_frames)
        else:
            media = prepare_video(path, output, max_frames)
        frames = {f['id']: f for f in media['frames']}
        parts = [{'type': 'text', 'text': '以下是同一段视频的时序图片；帧ID和时间仅是证据索引。'}]
        for f in media['frames']:
            parts += [{'type': 'text', 'text': f'frame_id={f["id"]}; media_time_ms={f["media_time_ms"]}'},
                      image_part(output / f['model_file'])]
        detected = structured(client, [{'role': 'system', 'content': prompt('detect')},
            {'role': 'user', 'content': parts}], 'detect_' + path.stem, Detection,
            lambda value: validate_detection(value, frames))
        write_json(output / 'detection.json', detected.model_dump())
        crops_dir = output / 'crops'
        crops_dir.mkdir(exist_ok=True)
        evidence, verification_parts, annotated = {}, [], {}
        for index, obj in enumerate(detected.objects):
            verification_parts.append({'type': 'text', 'text': f'candidate_id={obj.candidate_id}; 前一步仅提出候选，下面独立复核其截图。'})
            for vi, (view, quality) in enumerate(rank_views(obj.views, frames, output)):
                f = frames[view.frame_id]
                source = Image.open(output / f['file'])
                box = pixel_box(view.bbox, *source.size)
                crop = source.crop(box)
                crop_id = f'{path.stem}-o{index:02d}-c{vi}'
                crop_file = f'crops/{crop_id}.jpg'
                crop.save(output / crop_file, quality=95)
                evidence[crop_id] = {'id': crop_id, 'candidate_id': obj.candidate_id, 'file': crop_file,
                    'frame_id': f['id'], 'media_time_ms': f['media_time_ms'],
                    'bbox_normalized_xyxy': view.bbox, 'crop_pixel_xyxy': list(box),
                    'source_size': list(source.size), 'crop_size': list(crop.size), 'crop_quality': quality}
                proxy = crop.copy()
                proxy.thumbnail((1000, 1000))
                model_crop = output / f'crops/{crop_id}-model.jpg'
                proxy.save(model_crop, quality=92)
                verification_parts += [{'type': 'text', 'text': f'candidate_id={obj.candidate_id}; evidence_id={crop_id}'}, image_part(model_crop)]
                if f['id'] not in annotated:
                    annotated[f['id']] = source.copy()
                draw = ImageDraw.Draw(annotated[f['id']])
                draw.rectangle(box, outline='#ff3030', width=max(3, source.width // 400))
                draw.text((box[0], max(0, box[1] - 20)), obj.candidate_id, fill='#ff3030', stroke_width=1)
        (output / 'annotated').mkdir(exist_ok=True)
        for frame_id, im in annotated.items():
            im.save(output / f'annotated/{frame_id}.jpg', quality=93)
        write_json(output / 'evidence.json', evidence)
        result['evidence'] = evidence
        if detected.objects:
            verified = structured(client, [{'role': 'system', 'content': prompt('verify')},
                {'role': 'user', 'content': verification_parts}], 'verify_' + path.stem, Verification,
                lambda value: validate_verification(value, evidence))
            write_json(output / 'verification.json', verified.model_dump())
            accepted = [i for i in verified.items if i.target_kind == 'movable_item' and i.localization_status != 'wrong_target']
            result['rejected_candidates'] = [i.model_dump() for i in verified.items if i not in accepted]
            normalized, mode = normalize(client, [i.model_dump() for i in accepted], ToolBox(evidence), output) if accepted else ({'items': []}, 'not_needed')
            mapping = {i['candidate_id']: i for i in normalized['items']}
            result['retrieval_mode'] = mode
            for obj in accepted:
                item = guard_identity(obj.model_dump())
                info = mapping[obj.candidate_id]
                item['category'] = info['category']
                item['retrieval'] = info
                times = [evidence[e]['media_time_ms'] for e in obj.evidence_refs]
                item.update(observation_id=f'{path.stem}:{obj.candidate_id}', identity_status='candidate',
                    first_selected_view_ms=min(times), last_selected_view_ms=max(times),
                    first_seen_at=None, stored_at=None, put_in_at=None, quantity=None,
                    production_on=None, expires_on=None, presence_status='uncertain')
                item['time_note'] = 'Selected views only; not the actual first appearance or insertion time.'
                item['field_meta'] = {key: {'source': 'vision', 'certainty': 'estimated',
                    'evidence_refs': obj.evidence_refs, 'extractor': client.settings.model}
                    for key in ('name', 'description', 'category', 'brand', 'variant') if item.get(key) is not None}
                item['field_meta']['category']['supporting_sources'] = info['catalog_refs']
                if obj.localization_status in ('partial', 'wrong_target', 'multiple_objects', 'uncertain'):
                    item['needs_review'] = True
                else:
                    item['needs_review'] = item['category_support'] != 'high' or item['identity_support'] != 'high'
                result['items'].append(item)
            result['possible_duplicate_links'] = possible_duplicates(result['items'], evidence)
            if result['items']:
                try:
                    event_parts = [{'type': 'text', 'text': json.dumps({'candidates': [
                        {k: i[k] for k in ('candidate_id', 'name', 'category', 'description')}
                        for i in result['items']]}, ensure_ascii=False)}] + parts

                    events = structured(client, [{'role': 'system', 'content': prompt('events')},
                        {'role': 'user', 'content': event_parts}], 'events_' + path.stem, Events,
                        lambda value: validate_events(value, frames, {i['candidate_id'] for i in result['items']}), max_tokens=3200)
                    result['event_candidates'] = []
                    for e in events.events:
                        event = e.model_dump()
                        event.update(event_window_ms=[frames[e.before_frame_id]['media_time_ms'],
                            frames[e.after_frame_id]['media_time_ms']], committed=False, stored_at=None,
                            certainty='estimated', source='vision')
                        result['event_candidates'].append(event)
                    result['event_uncertainties'] = events.uncertainties
                except (APIError, ValueError) as exc:
                    result['errors'].append('Optional event stage: ' + client.safe(exc))
        result['uncertainties'] = detected.uncertainties
        result['status'] = 'completed'
    except (APIError, ValueError, OSError) as exc:
        result['status'] = 'failed'
        result['errors'].append(client.safe(exc))
    result['elapsed_s'] = round(time.monotonic() - started, 2)
    write_json(result_path, result)
    print(path.name, result['status'], [(i['name'], i['category']) for i in result['items']], flush=True)
    return result


def render_report(results, output):
    esc = lambda v: html.escape(str(v))
    sections = []
    for result in results:
        stem = Path(result['video']).stem
        items = []
        for item in result['items']:
            evidence = result.get('evidence', {})
            refs = [evidence[e] for e in item['evidence_refs'] if e in evidence]
            images = ''.join(f'<figure><a href="{esc(stem + "/" + e["file"])}"><img loading="lazy" src="{esc(stem + "/" + e["file"])}" alt="{esc(e["id"])}"></a><figcaption>{e["media_time_ms"] / 1000:.2f}s · <a href="{esc(stem + "/annotated/" + e["frame_id"] + ".jpg")}">原帧标框</a></figcaption></figure>' for e in refs)
            item_info = esc(json.dumps({k: item[k] for k in ('name', 'category', 'description', 'brand', 'variant', 'readable_text', 'category_support', 'identity_support', 'support_reason', 'localization_status', 'put_in_at')}, ensure_ascii=False, indent=2))
            items.append(f'<article><h3>{esc(item["name"])}</h3><div class="crops">{images}</div><pre>{item_info}</pre></article>')
        errors = ''.join(f'<p class="error">{esc(e)}</p>' for e in result['errors'])
        event_rows = ''.join(f'<tr><td>{esc(e["kind"])}</td><td>{esc(", ".join(e["candidate_ids"]))}</td><td>{e["event_window_ms"][0] / 1000:.3f}–{e["event_window_ms"][1] / 1000:.3f}s</td><td>{esc(e["explanation"])}</td></tr>' for e in result.get('event_candidates', []))
        event_table = f'<h3>动作候选 · 待核实</h3><div class="table-scroll"><table><thead><tr><th>动作</th><th>候选ID</th><th>媒体时间窗</th><th>依据与疑点</th></tr></thead><tbody>{event_rows}</tbody></table></div>' if event_rows else '<p>未生成动作候选。</p>'
        sections.append(f'<section><h2>{esc(result["video"])} · {esc(result["status"])} · {result.get("elapsed_s", 0)}s</h2><a href="{esc(stem)}/contact-sheet.jpg"><img class="sheet" src="{esc(stem)}/contact-sheet.jpg" alt="contact sheet"></a>{errors}<div class="items">{"".join(items)}</div>{event_table}<p><a href="{esc(stem)}/result.json">物品JSON</a> · <a href="{esc(stem)}/evidence.json">截图证据</a> · <a href="{esc(stem)}/media.json">时间和选帧</a></p></section>')
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Mind Box 视频实测</title><style>
body{font:15px/1.6 system-ui,sans-serif;margin:0;color:#172326;background:#fff}header,section{padding:24px max(20px,calc((100% - 1200px)/2));border-bottom:1px solid #ccc}h1{font-size:26px}h2{font-size:21px}h3{font-size:18px}img.sheet{width:100%;height:auto}.items{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,350px),1fr));gap:24px}article{min-width:0}pre{font:13px/1.6 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere}.crops{display:flex;gap:8px;min-height:250px}.crops figure{flex:1;min-width:0;margin:0}.crops img{width:100%;height:220px;object-fit:contain;background:#eee}figcaption{font-size:12px;overflow-wrap:anywhere}.table-scroll{overflow:auto}table{border-collapse:collapse;width:100%;min-width:540px}th,td{padding:8px;border-bottom:1px solid #ccc;text-align:left;vertical-align:top}td:nth-child(3){white-space:nowrap}.error{color:#b52424}a{color:#08716d}</style><header><h1>Mind Box · 视频实测</h1><p>物品观察候选；时间、库存数量和品牌未知时留空。high / medium / low 表示证据强度。</p></header>'''
    (output / 'report.html').write_text(page + ''.join(sections) + '</html>', encoding='utf-8')


def batch(input_path, output, max_frames=8, resume=False, model=None):
    output = Path(output)
    settings = Settings.load()
    if model:
        settings.model = model
    client = Client(settings, output)
    results = []
    for path in video_files(input_path):
        results.append(process(path, output / path.stem, client, max_frames, resume))
        write_json(output / 'summary.json', results)
        render_report(results, output)
    return results
