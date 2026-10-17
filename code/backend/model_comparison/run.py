"""Independent comparison entry point. No database, ASR, or production config writes."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import time

import httpx
from PIL import Image, ImageDraw
from pydantic import BaseModel, ConfigDict, Field

from app.client import Client, content, image_part, write_json
from app.config import ROOT, Settings
from model_comparison.dense_transport import structured
from model_comparison.responses_client import ResponsesClient

WORKSPACE = ROOT.parents[1]
BASE_URL = 'https://api.opentech.top/v1'


def digest(path):
    return hashlib.file_digest(Path(path).open('rb'), 'sha256').hexdigest()


def protected_hashes():
    paths = [ROOT / '.env', ROOT / 'var/mind-box.sqlite3', ROOT / 'var/mind-box.sqlite3-wal',
             ROOT / 'requirements.txt', ROOT / 'requirements.lock.txt']
    paths += sorted((ROOT / 'app').rglob('*.py')) + sorted((ROOT / 'app/prompts').glob('*.txt'))
    return {str(p.relative_to(ROOT)): digest(p) if p.exists() else None for p in paths}


def gpt_settings(model):
    key = os.environ.get('MIND_BOX_COMPARE_KEY', '')
    if not key:
        raise RuntimeError('MIND_BOX_COMPARE_KEY required; never stored in files')
    return Settings(BASE_URL + '/responses', key, model, attempts=2, timeout=240)


def probe(root):
    root.mkdir(parents=True, exist_ok=True)
    settings = gpt_settings('gpt-6.1-sol')
    with httpx.Client(timeout=60) as http:
        result = http.get(BASE_URL + '/models', headers={'Authorization': 'Bearer ' + settings.api_key})
    if result.status_code != 200:
        raise RuntimeError('Model discovery HTTP ' + str(result.status_code))
    models = sorted(row['id'] for row in result.json()['data'])
    write_json(root / 'models.json', {'model_ids': models, 'checked_at': datetime.now(timezone.utc).isoformat()})
    relevant = [m for m in models if '6.1' in m or '5.5' in m]
    print(json.dumps({'relevant_model_ids': relevant}, ensure_ascii=False), flush=True)
    image = Image.new('RGB', (360, 240), 'white')
    ImageDraw.Draw(image).rectangle((70, 50, 270, 190), fill='red')
    image.save(root / 'probe.jpg')
    wanted = [m for m in models if m.lower().replace('-', '').replace('.', '').replace('_', '') == 'gpt61sol']
    if not wanted:
        write_json(root / 'probe.json', {'requested_model': 'gpt6.1sol', 'status': 'unavailable',
                                       'relevant_model_ids': relevant})
        return
    model = wanted[0]
    client = ResponsesClient(gpt_settings(model), root / 'api')
    response = client.chat([{'role': 'user', 'content': [{'type': 'text', 'text':
        '观察图像，只返回JSON：{"color":"主要形状颜色","shape":"形状"}。'}, image_part(root / 'probe.jpg')]}],
        'vision_probe', max_tokens=600, json_mode=True, use_cache=False)
    parsed = json.loads(content(response))
    write_json(root / 'probe.json', {'requested_model': model, 'actual_model': response['model'],
                                   'status': 'responded', 'vision': parsed})
    print(json.dumps({'requested_model': model, 'actual_model': response['model'], 'vision': parsed}, ensure_ascii=False), flush=True)


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')


class DenseItem(Strict):
    object_id: str
    name: str
    category: str
    description: str
    readable_text: list[str]
    evidence_frame_ids: list[str]
    identity_uncertain: bool


class DenseEvent(Strict):
    object_id: str
    kind: str
    before_frame_id: str
    after_frame_id: str
    explanation: str


class DenseNet(Strict):
    object_id: str
    change: str


class DenseResult(Strict):
    objects: list[DenseItem] = Field(max_length=20)
    events: list[DenseEvent] = Field(max_length=30)
    net_changes: list[DenseNet] = Field(max_length=20)
    uncertainties: list[str]


DENSE_PROMPT = '''你是收纳柜视频分析员。输入只有同一视频按时间排序的完整画面，帧ID和媒体时间。
观察实物，不把抽屉、分隔板、手或包装正反面当成新增物品。对每件实物分配稳定object_id；
同一件离开又回来沿用ID。同款不同件不能只靠品名合并。允许只认出大类，读不到的品牌不猜。
基于相对抽屉的运动判断放入/取出；抽屉移动、遮挡或出画不自动表示物品取出。
events按发生顺序逐步列出add/remove/return/move/uncertain，不用只报最终净变化。
每条引用真实输入的before_frame_id与after_frame_id，时间必须增加。
net_changes覆盖全部objects，change只用add/remove/kept/not_present/unknown。
反复取出放回的物品最终还在时，净变化为kept。信息不足保留unknown和uncertainties。
本次不处理音频、生产日期、提醒或库存写入，只分析画面。只输出以下schema规定的JSON。
'''


def dense(video, destination, client, *, frozen_source=None, presentation='individual'):
    destination.mkdir(parents=True, exist_ok=True)
    existing = destination / 'result.json'
    if existing.exists():
        return json.loads(existing.read_text(encoding='utf-8'))
    start = time.monotonic()
    if frozen_source is not None:
        metadata = json.loads((frozen_source / 'frames.json').read_text(encoding='utf-8'))
        if metadata['input_sha256'] != digest(video) or metadata['prompt_sha256'] != hashlib.sha256(DENSE_PROMPT.encode()).hexdigest():
            raise ValueError('Frozen video or prompt differs')
        frames = metadata['frames']
        for frame in frames:
            source = (frozen_source / frame['file']).resolve()
            target = (destination / frame['file']).resolve()
            if not source.is_relative_to(frozen_source.resolve()) or not target.is_relative_to(destination.resolve()):
                raise ValueError('Frame path escapes experiment')
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if digest(source) != digest(target):
                raise ValueError('Frozen image copy differs')
        shutil.copyfile(frozen_source / 'frames.json', destination / 'frames.json')
        shutil.copyfile(frozen_source / 'contact-sheet.jpg', destination / 'contact-sheet.jpg')
    else:
        from app.media import action_frames, sheet

        frames = action_frames(video, destination, fps=4, max_frames=240)
        write_json(destination / 'frames.json', {'sampling_fps': 4, 'frames': frames,
                   'input_sha256': digest(video), 'prompt_sha256': hashlib.sha256(DENSE_PROMPT.encode()).hexdigest()})
        sheet(frames, destination, destination / 'contact-sheet.jpg')
    parts = [{'type': 'text', 'text': '以下是完整时间范围的均匀密集帧；没有额外库存上下文。'}]
    for frame in frames:
        parts += [{'type': 'text', 'text': f'frame_id={frame["id"]}; media_time_ms={frame["media_time_ms"]}'},
                  image_part(destination / frame['file'])]
    if presentation == 'pairs':
        from model_comparison.paired_frames import paired_parts

        parts = paired_parts(frames, destination)
    elif presentation != 'individual':
        raise ValueError('Unknown frame presentation')

    def validate(value):
        objects = {o.object_id for o in value.objects}
        times = {f['id']: f['media_time_ms'] for f in frames}
        if len(objects) != len(value.objects):
            raise ValueError('Duplicate object IDs')
        for obj in value.objects:
            if not obj.evidence_frame_ids or not set(obj.evidence_frame_ids) <= times.keys():
                raise ValueError('Invalid evidence frames')
        for event in value.events:
            if event.object_id not in objects or event.kind not in ('add', 'remove', 'return', 'move', 'uncertain'):
                raise ValueError('Invalid event')
            if event.before_frame_id not in times or event.after_frame_id not in times or times[event.before_frame_id] >= times[event.after_frame_id]:
                raise ValueError('Invalid event time')
        if sorted(n.object_id for n in value.net_changes) != sorted(objects):
            raise ValueError('Net must cover each object once')
        if any(n.change not in ('add', 'remove', 'kept', 'not_present', 'unknown') for n in value.net_changes):
            raise ValueError('Invalid net change')

    result = {'video': video.name, 'requested_model': client.settings.model, 'status': 'running'}
    try:
        value = structured(client, [{'role': 'system', 'content': DENSE_PROMPT + json.dumps(DenseResult.model_json_schema(), ensure_ascii=False)},
                                   {'role': 'user', 'content': parts}], 'dense_' + video.stem, DenseResult, validate, max_tokens=8000)
        result.update(status='completed', **value.model_dump(), frame_count=len(frames))
    except Exception as exc:
        result.update(status='failed', error=client.safe(exc), frame_count=len(frames))
    result['elapsed_s'] = round(time.monotonic() - start, 2)
    write_json(existing, result)
    print('dense', video.stem, result['status'], 'frames=' + str(len(frames)), flush=True)
    return result


def run(root, model, groups, videos):
    root.mkdir(parents=True, exist_ok=True)
    os.environ['MIND_BOX_WEB_SEARCH'] = 'false'
    os.environ.pop('MIND_BOX_CATALOG', None)
    manifest_file = root / 'manifest.json'
    if manifest_file.exists():
        manifest = json.loads(manifest_file.read_text(encoding='utf-8'))
        if manifest['protected_before'] != protected_hashes():
            raise RuntimeError('Protected project inputs changed; create a new experiment')
    else:
        manifest = {'created_at': datetime.now(timezone.utc).isoformat(), 'model': model,
                    'protected_before': protected_hashes(), 'inputs': {v: digest(WORKSPACE / 'data' / (v + '.mp4')) for v in videos},
                    'scope': '10 development videos, visual tasks only, no DB/ASR/delivery, single pass per arm',
                    'pipeline_max_frames': 10, 'dense_fps': 4, 'reasoning_effort': 'low',
                    'gpt_output_budget': 'original max_tokens + 4000 for Responses reasoning',
                    'evaluation_is_post_inference': True, 'initial_inventory': None}
        write_json(manifest_file, manifest)
    for group in groups:
        destination = root / group
        if group == 'deepseek_pipeline':
            settings = Settings.load()
            settings.model, settings.attempts = 'deepseek-pro', 2
            client = Client(settings, destination / 'api')
        else:
            client = ResponsesClient(gpt_settings(model), destination / 'api')
        results = []
        for name in videos:
            video = WORKSPACE / 'data' / (name + '.mp4')
            if manifest['inputs'][name] != digest(video):
                raise RuntimeError('Input video changed')
            print(group, name, 'starting', flush=True)
            if group == 'gpt_dense':
                result = dense(video, destination / name, client)
            else:
                from app.pipeline import process

                result = process(video, destination / name, client, 10, resume=True)
            results.append(result)
            write_json(destination / 'summary.json', results)
    after = protected_hashes()
    write_json(root / 'isolation-check.json', {'unchanged': after == manifest['protected_before'],
               'protected_after': after})
    if after != manifest['protected_before']:
        raise RuntimeError('Protected files changed during experiment')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['probe', 'run'])
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--model', default='gpt-6.1-sol')
    parser.add_argument('--groups', nargs='+', choices=['deepseek_pipeline', 'gpt_pipeline', 'gpt_dense'],
                        default=['deepseek_pipeline', 'gpt_pipeline', 'gpt_dense'])
    parser.add_argument('--videos', nargs='+', default=[f'v{i}' for i in range(1, 11)])
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_relative_to(ROOT / 'var/experiments'):
        parser.error('Results must stay within backend/var/experiments')
    if args.mode == 'probe':
        probe(root)
    else:
        run(root, args.model, args.groups, args.videos)


if __name__ == '__main__':
    main()
