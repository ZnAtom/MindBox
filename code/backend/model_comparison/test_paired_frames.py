from PIL import Image

from model_comparison.paired_frames import paired_parts


def test_pair_pages_preserve_every_source_pixel_and_frame_order(tmp_path):
    frames = []
    for i, color in enumerate(('red', 'green', 'blue')):
        filename = f'{i}.jpg'
        Image.new('RGB', (64, 48), color).save(tmp_path / filename)
        frames.append({'id': f'f{i}', 'media_time_ms': i * 250, 'file': filename})
    parts = paired_parts(frames, tmp_path)
    assert len([p for p in parts if p['type'] == 'image_url']) == 2
    assert parts[1]['text'] == 'frame_id=f0; media_time_ms=0\nframe_id=f1; media_time_ms=250'
    for i, frame in enumerate(frames):
        with Image.open(tmp_path / frame['file']) as original, Image.open(tmp_path / f'paired-frames/page-{i // 2 + 1:02d}.png') as page:
            top = (i % 2) * 80 + 32
            assert page.crop((0, top, 64, top + 48)).tobytes() == original.convert('RGB').tobytes()
