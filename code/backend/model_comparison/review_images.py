"""Create labelled crop sheets for human post-inference identity review."""
import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def create(root):
    destination = root / 'review-crops'
    destination.mkdir(exist_ok=True)
    for group in ('deepseek_pipeline', 'gpt_pipeline'):
        for result_file in sorted((root / group).glob('v*/result.json')):
            result = json.loads(result_file.read_text(encoding='utf-8'))
            rows = []
            for item in result.get('items', []):
                refs = item.get('evidence_refs', [])[:3]
                rows.append((item['candidate_id'], [result['evidence'][r] for r in refs]))
            canvas = Image.new('RGB', (1080, max(1, len(rows)) * 280), 'white')
            draw = ImageDraw.Draw(canvas)
            for row, (candidate_id, evidence) in enumerate(rows):
                for column, crop in enumerate(evidence):
                    with Image.open(result_file.parent / crop['file']) as im:
                        im.thumbnail((350, 245))
                        canvas.paste(im, (column * 360, row * 280))
                    draw.text((column * 360 + 5, row * 280 + 248), candidate_id + ' ' + crop['id'],
                              fill='black', font=ImageFont.load_default(size=17))
            canvas.save(destination / (group + '-' + result_file.parent.name + '.jpg'), quality=95)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    create(parser.parse_args().root.resolve())
