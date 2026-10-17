import copy
import sqlite3

import pytest

from model_comparison.long_video import (Packet, apply_packet, check_event, plan_slices,
    save_database, validate_packet)


def state():
    return {'model': 'test', 'objects': {}, 'events': [], 'proposals': [],
            'baselines': [], 'conflicts': [], 'summaries': []}


def obj(oid='obj-001'):
    return {'object_id': oid, 'name': '白色袋装物品', 'category': '未知', 'description': '白色袋子',
            'readable_text': [], 'identity_support': 'medium'}


def point(i, region, rest=False, location='drawer-1:left', oid='obj-001'):
    box = [0, 100, 300, 400] if region == 'frame_edge' else [200, 200, 400, 500]
    return {'object_id': oid, 'frame_id': str(i), 'region': region,
            'bbox': None if region in ('occluded', 'out_of_frame') else box,
            'location': location, 'at_rest': rest, 'released': rest, 'drawer_phase': 'open'}


def event(kind, before, after):
    return {'object_id': 'obj-001', 'kind': kind, 'before_frame_id': str(before),
            'after_frame_id': str(after), 'evidence_frame_ids': [str(before), str(after)],
            'explanation': 'Test evidence', 'support': 'medium'}


def packet(points, events=(), objects=None):
    return {'summary': 'Test', 'objects': [obj()] if objects is None else objects,
            'observations': points, 'events': list(events), 'uncertainties': []}


def frames(n=50):
    return {str(i): {'id': str(i), 'media_time_ms': i * 250} for i in range(n)}


def part(start=0, end=6000, name='slice-001'):
    return {'id': name, 'core_start_ms': start, 'core_end_ms': end}


def test_slices_cover_each_core_frame_once_and_overlap_without_future():
    source = list(frames().values())
    plans = plan_slices(source)
    for f in source:
        ms = f['media_time_ms']
        assert sum(p['core_start_ms'] <= ms < p['core_end_ms'] for p in plans) == 1
    assert plans[1]['context_start_ms'] == 4000
    for p in plans:
        assert all(frames()[fid]['media_time_ms'] < p['core_end_ms'] for fid in p['frame_ids'])
    assert source[-1]['id'] in plans[-1]['frame_ids']


def test_cross_slice_placement_and_overlap_do_not_duplicate_item_or_action():
    s, fs = state(), frames()
    apply_packet(s, packet([point(20, 'frame_edge'), point(22, 'inside')]), fs, part())
    assert not s['events']
    second = packet([point(24, 'inside', True), point(25, 'inside', True)], [event('add', 20, 25)])
    apply_packet(s, second, fs, part(6000, 12000, 'slice-002'))
    assert [e['kind'] for e in s['events']] == ['add']
    assert not s['baselines']
    apply_packet(s, second, fs, part(6000, 12000, 'slice-002'))
    assert len(s['events']) == 1
    assert len(s['objects']) == 1


def test_initial_registration_then_remove_return_reuses_one_identity():
    s, fs = state(), frames()
    apply_packet(s, packet([point(0, 'inside', True), point(1, 'inside', True),
        point(2, 'frame_edge'), point(3, 'out_of_frame')], [event('remove', 0, 3)]), fs, part())
    assert len(s['baselines']) == 1 and not s['baselines'][0]['filmed_inventory_action']
    apply_packet(s, packet([point(5, 'frame_edge'), point(6, 'inside', True), point(7, 'inside', True)],
        [event('add', 5, 7)], objects=[]), fs, part())
    assert [e['kind'] for e in s['events']] == ['remove', 'return']
    assert len(s['objects']) == 1 and s['objects']['obj-001']['presence'] == 'inside'


def test_drawer_closing_is_not_removal_and_drawer_motion_is_not_object_move():
    s, fs = state(), frames()
    occluded = point(3, 'occluded')
    occluded['drawer_phase'] = 'closing'
    apply_packet(s, packet([point(0, 'inside', True), point(2, 'inside', True), occluded],
        [event('remove', 0, 3), event('move', 0, 2)]), fs, part())
    assert not s['events']
    assert s['objects']['obj-001']['presence'] == 'inside'


def test_move_changes_location_without_incrementing_inventory():
    s, fs = state(), frames()
    apply_packet(s, packet([point(0, 'inside', True),
        point(3, 'inside', True, 'drawer-2:right')], [event('move', 0, 3)]), fs, part())
    assert [e['kind'] for e in s['events']] == ['move']
    assert s['objects']['obj-001']['location'] == 'drawer-2:right'
    assert s['objects']['obj-001']['presence'] == 'inside'


def test_missing_video_boundary_remains_observation_not_confirmed_add():
    s, fs = state(), frames()
    apply_packet(s, packet([point(0, 'outside'), point(2, 'inside', True), point(3, 'inside', True)],
        [event('add', 0, 3)]), fs, part())
    assert not s['events']
    assert s['proposals'][0]['reason'] == 'entry_boundary_or_released_placement_missing'
    assert not s['baselines']


def test_overlap_disagreement_cannot_validate_conflicting_action():
    s, fs = state(), frames()
    apply_packet(s, packet([point(0, 'inside', True), point(2, 'occluded')]), fs, part())
    apply_packet(s, packet([point(2, 'frame_edge'), point(3, 'out_of_frame')],
        [event('remove', 0, 3)]), fs, part())
    assert not s['events'] and len(s['conflicts']) == 1
    assert s['proposals'][0]['reason'] == 'conflicting_region_evidence'


def test_schema_rejects_future_and_unseen_frames():
    fs = frames()
    value = Packet.model_validate(packet([point(1, 'inside', True)], [event('move', 1, 30)]))
    with pytest.raises(ValueError, match='beyond'):
        validate_packet(value, fs, {}, {}, part())
    with pytest.raises(ValueError, match='not delivered'):
        validate_packet(value, {}, fs, {}, part())


def test_analysis_database_rebuild_is_idempotent_and_stores_pending(tmp_path):
    s, fs = state(), frames()
    apply_packet(s, packet([point(0, 'inside', True), point(2, 'occluded')],
        [event('remove', 0, 2)]), fs, part())
    plan = {'config': {'input_sha256': 'f' * 64}}
    a = save_database(tmp_path, s, plan, 'completed')
    b = save_database(tmp_path, copy.deepcopy(s), plan, 'completed')
    assert a == b
    assert b['counts'] == {'objects': 1, 'observations': 2, 'proposals': 1, 'accepted_events': 0}
    with sqlite3.connect(tmp_path / 'analysis.sqlite3') as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'


def test_review_resolves_only_overlapping_proposals_of_same_object_and_action():
    from model_comparison.long_video_audit import unresolved
    earlier = {**event('add', 0, 3), 'event_window_ms': [0, 750], 'status': 'pending',
               'reason': 'entry_boundary_or_released_placement_missing'}
    accepted = {**event('add', 2, 5), 'event_window_ms': [500, 1250], 'event_id': 'complete'}
    unrelated = {**earlier, 'object_id': 'obj-002'}
    pending, resolved, duplicates = unresolved([earlier, unrelated], [accepted])
    assert pending == [unrelated]
    assert resolved[0]['resolved_by'] == 'complete' and not duplicates


def test_slice_resume_rebuilds_identical_database_without_model_calls(tmp_path, monkeypatch):
    from PIL import Image
    import model_comparison.long_video as module
    fs = frames(4)
    for frame in fs.values():
        frame.update(file=frame['id'] + '.jpg', sharpness=100)
        Image.new('RGB', (160, 90), 'white').save(tmp_path / frame['file'])
    plan = {'config': {'input_sha256': 'e' * 64}, 'frames': list(fs.values()), 'slices': [part()]}
    monkeypatch.setattr(module, 'prepare', lambda *args: plan)
    monkeypatch.setattr(module, 'gpt_settings', lambda *args: None)

    class FakeClient:
        def __init__(self, *args):
            pass

        def safe(self, error):
            return str(error)

    monkeypatch.setattr(module, 'ResponsesClient', FakeClient)
    calls = []

    def observe(*args):
        calls.append(1)
        return packet([point(0, 'inside', True), point(2, 'frame_edge'), point(3, 'out_of_frame')],
                      [event('remove', 0, 3)]), []

    monkeypatch.setattr(module, 'read_slice', observe)
    first = module.run('test.mp4', tmp_path)
    second = module.run('test.mp4', tmp_path)
    assert first == second and len(calls) == 1
    assert first['counts']['accepted_events'] == 1


def test_catalog_uses_early_placement_before_later_stacking(tmp_path):
    from PIL import Image
    from model_comparison.long_video_audit import catalog_views
    fs = frames(4)
    for f in fs.values():
        f['file'] = f['id'] + '.jpg'
        Image.new('RGB', (100, 100), 'white' if f['id'] == '1' else 'black').save(tmp_path / f['file'])
    original = {**obj(), 'points': [point(1, 'inside', True), point(3, 'inside', True)],
                'prototype': {'file': '3.jpg'}}
    reviewed = catalog_views([('obj-001', original)], [event('add', 0, 1)], fs, tmp_path)
    assert reviewed[0][1]['prototype']['frame_id'] == '1'
    assert original['prototype']['file'] == '3.jpg'
    with Image.open(tmp_path / reviewed[0][1]['prototype']['file']) as crop:
        assert crop.getpixel((0, 0))[0] > 245
