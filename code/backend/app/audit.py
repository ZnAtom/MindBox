import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from PIL import Image, ImageDraw

from .client import write_json


def audit(output):
    output = Path(output)
    results = json.loads((output / 'summary.json').read_text(encoding='utf-8'))
    attempts_path = output / 'api-attempts.jsonl'
    attempts = [json.loads(line) for line in attempts_path.read_text(encoding='utf-8').splitlines()] if attempts_path.exists() else []
    counts = Counter(a['status'] for a in attempts)
    stats = {'videos': len(results), 'video_statuses': dict(Counter(r['status'] for r in results)),
             'api_log_statuses': dict(counts), 'http_statuses': dict(Counter(str(a['http_status']) for a in attempts if 'http_status' in a)),
             'total_tokens': sum(a.get('usage', {}).get('total_tokens', 0) for a in attempts),
             'actual_model_aliases': sorted({a['actual_model'] for a in attempts if a.get('actual_model')}),
             'api_request_elapsed_s': round(sum(a.get('elapsed_s', 0) for a in attempts), 2),
             'failures': [a for a in attempts if a['status'] in ('failed', 'invalid_output')],
             'per_video': [], 'result_sha256': {}}
    gallery = []
    for r in results:
        stem = Path(r['video']).stem
        result_path = output / stem / 'result.json'
        stats['result_sha256'][str(result_path.relative_to(output))] = hashlib.sha256(result_path.read_bytes()).hexdigest()
        media = json.loads((output / stem / 'media.json').read_text(encoding='utf-8'))
        stats['per_video'].append({'video': r['video'], 'status': r['status'], 'duration_s': media['duration_s'],
            'resolution': [media['width'], media['height']], 'audio_streams': media['audio_streams'],
            'scan_frames': media['candidate_count'], 'selected_frames': len(media['frames']),
            'duplicate_frames_suppressed': len(media.get('duplicate_representatives_suppressed', [])),
            'crops': len(r.get('evidence', {})), 'candidate_count': len(r['items']),
            'rejected_candidates': len(r.get('rejected_candidates', [])),
            'duplicate_links': len(r.get('possible_duplicate_links', [])),
            'events': len(r.get('event_candidates', [])),
            'items': [{key: item.get(key) for key in ('candidate_id', 'name', 'category', 'brand', 'variant',
                'category_support', 'identity_support', 'localization_status', 'needs_review')}
                for item in r['items']], 'elapsed_s': r.get('elapsed_s'), 'errors': r['errors']})
        for item in r['items']:
            if not item['evidence_refs']:
                continue
            ev = r['evidence'][item['evidence_refs'][0]]
            gallery.append((stem + ':' + item['candidate_id'], output / stem / ev['file']))
    if gallery:
        w, h, cols = 320, 260, 4
        canvas = Image.new('RGB', (w * cols, h * math.ceil(len(gallery) / cols)), '#202020')
        draw = ImageDraw.Draw(canvas)
        for i, (label, path) in enumerate(gallery):
            im = Image.open(path)
            im.thumbnail((w, h - 25))
            x, y = (i % cols) * w, (i // cols) * h
            canvas.paste(im, (x, y))
            draw.text((x + 5, y + h - 22), label, fill='white')
        canvas.save(output / 'crop-gallery.jpg', quality=90)
    write_json(output / 'audit.json', stats)
    print(json.dumps({k: stats[k] for k in ('videos', 'video_statuses', 'api_log_statuses', 'total_tokens')}, ensure_ascii=False), flush=True)
    return stats
