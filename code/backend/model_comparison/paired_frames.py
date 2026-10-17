"""Lossless two-frame pages for gateways limited to 20 images per request."""
import base64
import hashlib
import json

from PIL import Image, ImageDraw

from app.client import write_json

INTRO = ('以下是完整时间范围的均匀密集帧；没有额外库存上下文。'
         '每张图片由相邻两帧按原尺寸上下拼接，末张可能只有一帧。'
         '每帧上方标有frame_id和media_time_ms；按图片顺序、图内从上到下观察。')


def paired_parts(frames, destination):
    parts = [{'type': 'text', 'text': INTRO}]
    pages = []
    folder = destination / 'paired-frames'
    folder.mkdir(exist_ok=True)
    for offset in range(0, len(frames), 2):
        pair = frames[offset:offset + 2]
        images = []
        for frame in pair:
            with Image.open(destination / frame['file']) as source:
                images.append(source.convert('RGB'))
        page = Image.new('RGB', (max(im.width for im in images), sum(im.height + 32 for im in images)), 'white')
        draw, top = ImageDraw.Draw(page), 0
        labels = []
        for frame, im in zip(pair, images):
            label = f'frame_id={frame["id"]}; media_time_ms={frame["media_time_ms"]}'
            draw.text((8, top + 8), label, fill='black', font_size=18)
            page.paste(im, (0, top + 32))
            labels.append(label)
            top += im.height + 32
        path = folder / f'page-{offset // 2 + 1:02d}.png'
        page.save(path)
        data = path.read_bytes()
        pages.append({'file': path.relative_to(destination).as_posix(), 'frame_ids': [f['id'] for f in pair],
                      'sha256': hashlib.sha256(data).hexdigest(), 'width': page.width, 'height': page.height})
        parts += [{'type': 'text', 'text': '\n'.join(labels)},
                  {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(data).decode('ascii')}}]
    write_json(destination / 'presentation.json', {'mode': 'pairs', 'source_frame_count': len(frames),
        'image_count': len(pages), 'resize': False, 'encoding': 'PNG', 'pages': pages,
        'intro_sha256': hashlib.sha256(INTRO.encode()).hexdigest(),
        'note': 'Original decoded JPEG RGB pixels pasted unchanged. Gateway may rescale internally.'})
    return parts
