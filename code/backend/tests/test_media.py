import av
import numpy as np

from app.media import action_frames, inspect_media, prepare_video


def test_static_video_suppresses_redundancy_and_preserves_endpoints(tmp_path):
    source = tmp_path / 'static.mp4'
    with av.open(str(source), 'w') as container:
        stream = container.add_stream('mpeg4', rate=12)
        stream.width, stream.height, stream.pix_fmt = 160, 120, 'yuv420p'
        for index in range(48):
            image = np.full((120, 160, 3), 80, dtype=np.uint8)
            image[30:90, 40:120] = [170, 60, 90]
            frame = av.VideoFrame.from_ndarray(image, format='rgb24')
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    meta = prepare_video(source, tmp_path / 'out', max_frames=8)
    assert meta['duplicate_representatives_suppressed']
    assert len(meta['frames']) < 8
    assert meta['frames'][0]['media_time_ms'] == 0
    assert meta['frames'][-1]['media_time_ms'] >= 3500
    assert len(meta['input_sha256']) == 64
    assert meta['container']['streams'][0]['codec'] == 'mpeg4'
    info = inspect_media(source)
    assert info['container']['streams'][0]['width'] == 160
    assert info['container']['streams'][0]['height'] == 120
    assert info['container']['streams'][0]['time_base'] != 'None'
    temporal = action_frames(source, tmp_path / 'action-out', fps=8)
    assert len(temporal) > len(meta['frames'])
    assert temporal[-1]['media_time_ms'] == round(47 / 12 * 1000)
