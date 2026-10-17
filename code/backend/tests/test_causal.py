import copy
import json

import av
import numpy as np
from PIL import Image
import pytest

from app.causal import (CausalLedger, DeliveredFrames, commit_candidates, observe,
                        persist_action, run, validate_observation)
from app.causal_schemas import CausalObservation
from app.experiments import Experiment
from app.media import signature, visual_quality
from app.policy import descriptive_identity
from app.quality import assess_quality


def point(fid, region, rest=False):
    box = [0, 100, 160, 400] if region == 'frame_edge' else [300, 200, 700, 650]
    return {'frame_id': fid, 'region': region, 'at_rest': rest,
            'bbox': box if region in ('inside', 'outside', 'frame_edge') else None}


def track(tid, points, **patch):
    return {'track_id': tid, 'name': '未知物品', 'category': '未知', 'description': '白色袋装物品',
            'target_kind': 'movable_item', 'object_support': 'medium', 'association': 'supported', 'visibility_fraction': 0.8,
            'occlusion_level': 'mild', 'points': points, **patch}


def packet(tracks, **patch):
    return {'drawer_phase': 'open', 'drawer_only': False, 'uncertainties': [], 'tracks': tracks, **patch}


def test_quality_bands_are_diagnostic_even_very_soft_recognizable_object():
    assert visual_quality(34, 0, 256, 144)['blur'] == 'severe'
    assert visual_quality(80, 0, 256, 144)['blur'] == 'soft'
    assert visual_quality(121, 0, 256, 144)['blur'] == 'clear'
    item = {'target_kind': 'movable_item', 'localization_status': 'usable', 'object_support': 'medium',
            'identity_support': 'low', 'evidence_refs': ['x'], 'occlusion_level': 'moderate'}
    evidence = {'x': {'crop_quality': {'sharpness': 12, 'bad_exposure': 0.1}, 'crop_size': [200, 180]}}
    assert assess_quality(item, evidence)['status'] == 'eligible_with_warning'
    item['object_support'] = 'low'
    assert assess_quality(item, evidence)['status'] == 'pending'


def test_partial_crop_is_warning_when_physical_object_is_recognizable():
    item = {'target_kind': 'movable_item', 'localization_status': 'partial',
            'object_support': 'medium', 'evidence_refs': [], 'visibility_fraction': 0.3}
    assert assess_quality(item, {})['status'] == 'eligible_with_warning'
    item['localization_status'] = 'multiple_objects'
    assert assess_quality(item, {})['status'] == 'pending'


@pytest.mark.parametrize('description', ['透明袋装物品', '黑色带线物品', '银色金属小工具', '红色布质物品', '蓝色塑料盒'])
def test_unknown_items_keep_diverse_visible_descriptions(description):
    value = descriptive_identity({'name': '未知物品', 'description': description, 'category': '未知'})
    assert value['name'] == description and value['category'] == '未知'


def test_blur_metric_decreases_for_blurred_same_object():
    import cv2
    image = np.zeros((144, 256, 3), dtype=np.uint8)
    image[:, ::8] = 255
    assert signature(image)[0] > signature(cv2.GaussianBlur(image, (17, 17), 5))[0]


def test_causal_four_actions_preserve_item_identity_and_no_duplicate_events():
    frames = {str(i): {'media_time_ms': i * 250} for i in range(22)}
    ledger = CausalLedger()
    other = point('0', 'inside', True)
    other['bbox'] = [720, 200, 980, 650]
    baselines, _ = ledger.apply(packet([track('a', [point('0', 'inside', True)]),
                                       track('b', [other], description='另一件物品')]), frames, True)
    # Baseline geometry is checked independently by the model-output guard.
    assert baselines == ['a', 'b']
    ledger.state['tracks']['a']['item_id'] = 'existing-a'
    ledger.state['tracks']['b']['item_id'] = 'existing-b'
    actions = [packet([track('a', [point('1', 'outside'), point('2', 'frame_edge'), point('3', 'out_of_frame')])]),
        packet([track('c', [point('4', 'frame_edge'), point('5', 'outside'), point('6', 'inside', True), point('7', 'inside', True)])]),
        packet([track('b', [point('8', 'outside'), point('9', 'frame_edge'), point('10', 'out_of_frame')])]),
        packet([track('a', [point('11', 'frame_edge'), point('12', 'outside'), point('13', 'inside', True), point('14', 'inside', True)])])]
    events = []
    for value in actions:
        _, observed = ledger.apply(value, frames)
        events += observed
        assert not ledger.apply(value, frames)[1]
    assert [e['kind'] for e in events] == ['remove', 'add', 'remove', 'return']
    assert ledger.state['tracks']['a']['item_id'] == 'existing-a'
    assert {tid: e['presence'] for tid, e in ledger.state['tracks'].items()} == {'a': True, 'b': False, 'c': True}


def test_closing_and_outside_without_video_exit_do_not_remove():
    frames = {str(i): {'media_time_ms': i * 250} for i in range(8)}
    ledger = CausalLedger()
    ledger.apply(packet([track('a', [point('0', 'inside', True)])]), frames, True)
    assert not ledger.apply(packet([track('a', [point('1', 'occluded')])], drawer_only=True, drawer_phase='closing'), frames)[1]
    assert ledger.state['tracks']['a']['presence'] is True
    assert not ledger.apply(packet([track('a', [point('2', 'outside'), point('3', 'out_of_frame')])]), frames)[1]
    assert ledger.state['tracks']['a']['pending'] == 'video_exit_boundary_not_observed'


def test_missing_entry_boundary_and_held_object_cannot_add():
    frames = {str(i): {'media_time_ms': i * 250} for i in range(8)}
    ledger = CausalLedger()
    assert not ledger.apply(packet([track('a', [point('0', 'outside'), point('1', 'inside', True), point('2', 'inside', True)])]), frames)[1]
    assert ledger.state['tracks']['a']['pending'] == 'video_entry_boundary_not_observed'
    ledger = CausalLedger()
    assert not ledger.apply(packet([track('a', [point('0', 'frame_edge'), point('1', 'inside')])]), frames)[1]
    assert ledger.state['tracks']['a']['pending'] == 'placement_not_released'


def test_future_and_invented_edge_are_rejected():
    tool = DeliveredFrames({'a': {'id': 'a', 'media_time_ms': 100}}, 100)
    assert tool.get_frames(0, 101)['status'] == 'rejected'
    assert tool.get_frames(0, 100)['frames'][0]['id'] == 'a'
    value = CausalObservation.model_validate(packet([track('a', [point('future', 'inside', True)])]))
    with pytest.raises(ValueError, match='not delivered'):
        validate_observation(value, {'now': {'media_time_ms': 100}})
    p = point('now', 'frame_edge')
    p['bbox'] = [300, 300, 500, 500]
    with pytest.raises(ValueError, match='not near'):
        validate_observation(CausalObservation.model_validate(packet([track('a', [p])])), {'now': {'media_time_ms': 100}})


def test_old_overlap_only_and_missing_target_kind_are_rejected():
    data = packet([track('a', [point('old', 'inside', True)])])
    with pytest.raises(ValueError, match='context only'):
        validate_observation(CausalObservation.model_validate(data), {'old': {'media_time_ms': 100}}, 100)
    del data['tracks'][0]['target_kind']
    with pytest.raises(ValueError, match='target_kind'):
        CausalObservation.model_validate(data)


def test_closing_later_in_window_does_not_hide_earlier_placement():
    frames = {str(i): {'media_time_ms': i * 250} for i in range(5)}
    points = [point('0', 'frame_edge'), point('1', 'outside'),
              point('2', 'inside', True), point('3', 'inside', True)]
    for p in points:
        p['drawer_phase'] = 'open'
    points.append({**point('4', 'occluded'), 'drawer_phase': 'closing'})
    _, events = CausalLedger().apply(packet([track('a', points)], drawer_phase='closing'), frames)
    assert [e['kind'] for e in events] == ['add']


def test_outside_box_touching_actual_video_edge_also_supplies_boundary_evidence():
    frames = {str(i): {'media_time_ms': i * 250} for i in range(4)}
    ledger = CausalLedger()
    initial = {**point('0', 'outside'), 'bbox': [800, 400, 1000, 800]}
    ledger.apply(packet([track('a', [initial])]), frames, True)
    _, events = ledger.apply(packet([track('a', [point('1', 'inside', True), point('2', 'inside', True)])]), frames)
    assert [e['kind'] for e in events] == ['add']
    assert events[0]['before_frame_id'] == '0'
    assert ledger.state['tracks']['a']['presence'] is True


def test_same_frame_conflict_cannot_be_cleared_by_later_or_uncertain_observation():
    frames = {str(i): {'media_time_ms': i * 250} for i in range(5)}
    ledger = CausalLedger()
    ledger.apply(packet([track('a', [point('0', 'inside', True)])]), frames, True)
    ledger.apply(packet([track('a', [point('0', 'inside'), point('1', 'inside', True)])]), frames)
    ledger.apply(packet([track('a', [point('2', 'inside', True)], association='uncertain')]), frames)
    _, events = ledger.apply(packet([track('a', [point('3', 'frame_edge'), point('4', 'out_of_frame')])]), frames)
    assert not events
    assert ledger.state['tracks']['a']['pending'] == 'same_frame_track_state_conflict'


def test_observation_includes_prototypes_in_twenty_image_budget(tmp_path):
    Image.new('RGB', (80, 80), 'gray').save(tmp_path / 'image.jpg')
    frames = [{'id': str(i), 'media_time_ms': i * 100, 'file': 'image.jpg'} for i in range(16)]
    ledger = CausalLedger()
    for i in range(8):
        ledger.state['tracks'][str(i)] = {'presence': True, 'item_id': str(i), 'pending': None,
            'points': [], 'latest_crop': {'file': 'image.jpg'}}

    class Client:
        def chat(self, messages, stage, **kwargs):
            images = [p for m in messages if isinstance(m['content'], list)
                      for p in m['content'] if p['type'] == 'image_url']
            assert len(images) == 20
            return {'choices': [{'message': {'content': json.dumps(packet([], drawer_only=True))}}]}

    assert observe(Client(), ledger, frames, tmp_path, False, 0)['tracks'] == []
    trace = json.loads((tmp_path / 'prompt-images-000.json').read_text())
    assert trace['image_count'] == trace['limit'] == 20


def test_real_sqlite_per_action_return_reuses_item_and_resume_is_idempotent(tmp_path):
    video = tmp_path / 'test.mp4'
    video.write_bytes(b'synthetic-source')
    context = Experiment.create(tmp_path, 'session-test', videos=[video])
    root = context.root / 'causal'
    root.mkdir()
    frames = {}
    for i in range(10):
        Image.new('RGB', (300, 200), 'gray').save(root / f'{i}.jpg')
        frames[str(i)] = {'id': str(i), 'file': f'{i}.jpg', 'media_time_ms': i * 250, 'pts': i}
    ledger = CausalLedger()
    ledger.apply(packet([track('a', [point('0', 'inside', True)])]), frames, True)
    baseline = persist_action(context, ledger, 'a', None, frames, root, 'parent', video, True)
    item_id = baseline['added_item_ids'][0]
    assert baseline['filmed_inventory_action'] is False
    _, events = ledger.apply(packet([track('a', [point('1', 'outside'), point('2', 'frame_edge'), point('3', 'out_of_frame')])]), frames)
    remove = persist_action(context, ledger, 'a', events[0], frames, root, 'parent', video)
    assert remove['removed_item_ids'] == [item_id]
    _, events = ledger.apply(packet([track('a', [point('4', 'frame_edge'), point('5', 'outside'), point('6', 'inside', True), point('7', 'inside', True)])]), frames)
    back = persist_action(context, ledger, 'a', events[0], frames, root, 'parent', video)
    assert back == persist_action(context, ledger, 'a', events[0], frames, root, 'parent', video)
    assert back['updated_item_ids'] == [item_id]
    item = context.store.get_item(item_id)
    assert item['name'] == '白色袋装物品'
    assert item['presence_status'] == 'present' and len(item['storage_visits']) == 2
    assert context.store.db.status()['integrity'] == 'ok'


def baseline_fixture(tmp_path, name):
    video = tmp_path / 'input.mp4'
    video.write_bytes(b'isolated-source')
    context = Experiment.create(tmp_path, name, videos=[video])
    root = context.root / 'causal'
    root.mkdir()
    Image.new('RGB', (300, 200), 'gray').save(root / '0.jpg')
    frames = {'0': {'id': '0', 'file': '0.jpg', 'media_time_ms': 0, 'pts': 0}}
    a, b = point('0', 'inside', True), point('0', 'inside', True)
    b['bbox'] = [720, 200, 980, 650]
    data = packet([track('a', [a]), track('b', [b], description='蓝色塑料盒')])
    return video, context, root, frames, data


def test_one_failed_verification_does_not_discard_other_initial_item(tmp_path):
    video, context, root, frames, data = baseline_fixture(tmp_path, 'per-object')
    ledger = CausalLedger()
    before = copy.deepcopy(ledger.state)
    baselines, events = ledger.apply(data, frames, True)

    class Client:
        safe = staticmethod(str)

    def verify(client, ledger, tid, *args):
        if tid == 'a':
            raise ValueError('wrong target')

    outcome = commit_candidates(context, ledger, before, baselines, events, frames,
        root, 'parent', video, Client(), verifier=verify)
    assert list(outcome['verification_failures']) == ['a']
    assert len(outcome['receipts']) == 1
    assert ledger.state['tracks']['a']['presence'] is None
    assert ledger.state['tracks']['b']['item_id']
    snapshot = context.store.get_inventory_snapshot()
    assert snapshot['inventory_revision'] == 1
    assert [i['name'] for i in snapshot['items']] == ['蓝色塑料盒']


def test_partial_database_commit_replay_recovers_without_duplicates(tmp_path, monkeypatch):
    import app.causal as causal
    video, context, root, frames, data = baseline_fixture(tmp_path, 'recover')
    ledger = CausalLedger()
    before = copy.deepcopy(ledger.state)
    baselines, events = ledger.apply(data, frames, True)
    original = causal.persist_action

    def interrupted(context, ledger, tid, *args, **kwargs):
        if tid == 'b':
            raise OSError('simulated process interruption')
        return original(context, ledger, tid, *args, **kwargs)

    monkeypatch.setattr(causal, 'persist_action', interrupted)
    with pytest.raises(OSError):
        commit_candidates(context, ledger, before, baselines, events, frames, root, 'parent', video)
    assert ledger.state == before
    assert context.store.get_inventory_snapshot()['inventory_revision'] == 1
    monkeypatch.setattr(causal, 'persist_action', original)
    baselines, events = ledger.apply(data, frames, True)
    result = commit_candidates(context, ledger, before, baselines, events, frames, root, 'parent', video)
    assert len(result['receipts']) == 2
    assert context.store.get_inventory_snapshot()['inventory_revision'] == 2
    assert context.store.db.status()['integrity'] == 'ok'


def test_decoder_and_observer_only_deliver_past_frames(tmp_path):
    video = tmp_path / 'stream.mp4'
    with av.open(str(video), 'w') as container:
        stream = container.add_stream('mpeg4', rate=12)
        stream.width, stream.height, stream.pix_fmt = 160, 120, 'yuv420p'
        for i in range(30):
            frame = av.VideoFrame.from_ndarray(np.full((120, 160, 3), i * 6, dtype=np.uint8), format='rgb24')
            for encoded in stream.encode(frame):
                container.mux(encoded)
        for encoded in stream.encode():
            container.mux(encoded)
    context = Experiment.create(tmp_path, 'causal-input', videos=[video])
    seen = []

    def observer(client, ledger, frames, root, bootstrap, step):
        watermark = max(f['media_time_ms'] for f in frames)
        assert ledger.state['watermark_ms'] < watermark
        assert all(int(p.stem.rsplit('t', 1)[1]) <= frames[-1]['pts'] for p in (root / 'action-frames').glob('*.jpg'))
        seen.append(watermark)
        return packet([])

    result = run(context, video, None, observer=observer)
    assert result['status'] == 'completed' and len(seen) >= 3 and seen == sorted(seen)
    assert seen[-1] == round(29 / 12 * 1000)
    assert run(context, video, None, observer=observer) == result
