import json

import av

from app.client import Client
from app.config import Settings
from app.video_probe import make_controls, response_url, temporal_check, video_part


def test_temporal_control_requires_full_order_and_readability():
    phases = [{'color': 'blue', 'shape': 'circle'}, {'color': 'red', 'shape': 'triangle'}]
    assert temporal_check(json.dumps({'phases': phases, 'media_readable': True}), phases)['passed']
    for wrong in (list(reversed(phases)), phases[:1], []):
        assert not temporal_check(json.dumps({'phases': wrong, 'media_readable': True}), phases)['passed']
    assert not temporal_check(json.dumps({'phases': phases, 'media_readable': False}), phases)['passed']
    assert not temporal_check('I can read videos', phases)['passed']


def test_control_mp4_decodes_and_reverse_changes_order(tmp_path):
    clips = make_controls(tmp_path)
    assert clips[0]['expected'] == list(reversed(clips[1]['expected']))
    assert clips[0]['sha256'] != clips[1]['sha256']
    for clip in clips:
        with av.open(str(tmp_path / clip['file'])) as source:
            frames = list(source.decode(video=0))
            assert len(frames) == 24
            assert frames[-1].time > 2.8
            assert source.streams.video[0].codec_context.name == 'h264'
        part = video_part(tmp_path / clip['file'])
        assert part['type'] == 'video_url'
        assert part['video_url']['url'].startswith('data:video/mp4;base64,')


def test_media_and_key_redacted_before_truncation(tmp_path):
    client = Client(Settings('https://example.invalid', 'secret-key', 'test'), tmp_path)
    for kind in ('image/jpeg', 'video/mp4', 'audio/wav'):
        text = f'secret-key data:{kind};base64,' + 'A' * 2000 + ' end'
        safe = client.safe(text)
        assert 'secret-key' not in safe and 'AAAA' not in safe
        assert safe.endswith(' end')


def test_sibling_response_url():
    assert response_url('https://example.invalid/api/v1/start/chat/completions') == 'https://example.invalid/api/v1/response'
