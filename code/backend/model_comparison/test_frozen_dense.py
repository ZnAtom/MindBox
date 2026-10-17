"""Frozen images must be reused even when the production frame extractor changes."""
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from app.client import image_part, write_json
from model_comparison import run


def fixture_files(tmp_path):
    video = tmp_path / 'v1.mp4'
    video.write_bytes(b'frozen video')
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'frame.jpg').write_bytes(b'exact original jpeg bytes')
    (source / 'contact-sheet.jpg').write_bytes(b'sheet')
    metadata = {'input_sha256': run.digest(video),
                'prompt_sha256': hashlib.sha256(run.DENSE_PROMPT.encode()).hexdigest(),
                'sampling_fps': 4, 'frames': [{'id': 'v1-t63', 'media_time_ms': 1, 'file': 'frame.jpg'}]}
    write_json(source / 'frames.json', metadata)
    return video, source


def test_frozen_images_reuse_exact_bytes_ids_times_and_prompt(tmp_path, monkeypatch):
    video, source = fixture_files(tmp_path)
    captured = []

    def structured(client, messages, stage, schema, validate, **kwargs):
        captured.append(messages)
        result = run.DenseResult(objects=[], events=[], net_changes=[], uncertainties=[])
        validate(result)
        return result

    def forbidden(*args, **kwargs):
        raise AssertionError('Production frame extractor must not run')

    monkeypatch.setattr(run, 'structured', structured)
    monkeypatch.setitem(sys.modules, 'app.media', SimpleNamespace(action_frames=forbidden, sheet=forbidden))
    result = run.dense(video, tmp_path / 'output', SimpleNamespace(settings=SimpleNamespace(model='deepseek-pro')),
                       frozen_source=source)
    assert result['status'] == 'completed'
    assert captured[0][0]['content'] == run.DENSE_PROMPT + json.dumps(run.DenseResult.model_json_schema(), ensure_ascii=False)
    assert captured[0][1]['content'][1:] == [
        {'type': 'text', 'text': 'frame_id=v1-t63; media_time_ms=1'}, image_part(source / 'frame.jpg')]
    assert (tmp_path / 'output/frame.jpg').read_bytes() == (source / 'frame.jpg').read_bytes()


def test_changed_video_is_rejected_before_model_request(tmp_path):
    video, source = fixture_files(tmp_path)
    video.write_bytes(b'changed video')
    with pytest.raises(ValueError, match='Frozen video or prompt differs'):
        run.dense(video, tmp_path / 'output', None, frozen_source=source)
