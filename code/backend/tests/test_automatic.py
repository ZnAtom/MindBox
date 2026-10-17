import io
import json
import time
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from app.automatic import AutomaticRecorder, auto_settings, location_at
from app.capture import BridgeCapture, Sessions
from app.client import write_json
from app.config import Settings
from app.database import Conflict
from app.server import create_app
from app.store import Store


def jpeg():
    stream = io.BytesIO()
    Image.new('RGB', (320, 180), 'green').save(stream, 'JPEG')
    return stream.getvalue()


def services(tmp_path):
    store = Store(tmp_path / 'inventory.sqlite3')
    store.initialize()
    return store, Sessions(store, tmp_path / 'sessions')


def test_production_configuration_does_not_reuse_temporary_comparison_key(monkeypatch):
    monkeypatch.setenv('MIND_BOX_AUTO_API_KEY', '')
    monkeypatch.setenv('MIND_BOX_COMPARE_KEY', 'temporary-test-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'unrelated-provider-key')
    with pytest.raises(ValueError, match='MIND_BOX_AUTO_API_KEY'):
        auto_settings()
    monkeypatch.setenv('MIND_BOX_AUTO_API_KEY', 'explicit-auto-key')
    assert auto_settings().api_key == 'explicit-auto-key'


def capture_session(sessions, *, drawer=2, opened_at=2, close_at=4, tail_ms=500):
    now = [0.]
    capture = BridgeCapture(sessions, clock=lambda: now[0], tail_ms=tail_ms)
    frames = []
    for index in range(int((close_at + tail_ms / 1000 + 2) * 4)):
        now[0] = index / 4
        frame = {'generation': index + 1, 'received_ms': index * 250,
                 'received_unix_ms': round(time.time() * 1000)}
        frames.append(frame)
        opened = [False] * 3
        if opened_at <= now[0] < close_at:
            opened[drawer - 1] = True
        capture.step({'cabinet_id': 'cabinet-01', 'connected': True, 'state': {
            'source': 'vision', 'boot_id': 'boot', 'open': opened, 'known': [True] * 3}},
            {'epoch': 'camera-boot', 'frames': frames[-9:]}, lambda _: jpeg())
    return sessions.list()[0]


def packet(kind='add', *, oid='obj-001', existing=False):
    obj = {'object_id': oid, 'name': '绿色盒装物品', 'category': '日用品', 'description': '绿色长方形盒子',
           'readable_text': [], 'identity_support': 'high'}
    # The session has preroll; action starts after visual drawer confirmation (t=2s).
    regions = [('inside', True), ('frame_edge', False), ('out_of_frame', False)] if kind == 'remove' else [
        ('frame_edge', False), ('inside', True), ('inside', True)]
    points = []
    for index, (region, rest) in enumerate(regions, 9):
        points.append({'object_id': oid, 'frame_id': 'f%08d' % index, 'region': region,
                       'bbox': None if region == 'out_of_frame' else [0, 100, 300, 600] if region == 'frame_edge'
                       else [200, 100, 700, 800], 'location': 'drawer-1:left',
                       'at_rest': rest, 'released': rest, 'drawer_phase': 'open'})
    return {'summary': '实际动作', 'objects': [obj], 'observations': points, 'events': [{
        'object_id': oid, 'kind': kind, 'before_frame_id': points[0]['frame_id'],
        'after_frame_id': points[-1]['frame_id'], 'evidence_frame_ids': [p['frame_id'] for p in points],
        'explanation': '完整边界和放稳证据', 'support': 'high'}], 'uncertainties': []}


def recorder(sessions, value, calls=None):
    def reader(*args):
        if calls is not None:
            calls.append(1)
        return value, []
    return AutomaticRecorder(sessions, slice_reader=reader, client_factory=lambda *args: None,
        settings_factory=lambda: Settings('unused', 'fake', 'offline-test'))


def test_capture_keeps_preroll_whole_operation_and_tail_on_disk(tmp_path):
    _, sessions = services(tmp_path)
    session = capture_session(sessions)
    manifest = json.loads((Path(session['root']) / 'session.json').read_text())
    assert session['state'] == 'queued'
    assert manifest['complete']
    assert len(manifest['frames']) == 19
    assert manifest['frames'][0]['received_ms'] == 0
    assert manifest['frames'][-1]['received_ms'] == 4500
    assert location_at(manifest, 2500) == 'cabinet-01:drawer-2'
    assert location_at(manifest, 4100) is None
    assert all((Path(session['root']) / f['file']).exists() for f in manifest['frames'])


def test_real_sqlite_add_remove_return_reuses_identity_and_is_idempotent(tmp_path):
    store, sessions = services(tmp_path)
    first = capture_session(sessions)
    calls = []
    worker = recorder(sessions, packet(), calls)
    worker.process(first['id'])
    snapshot = store.get_inventory_snapshot()
    assert snapshot['inventory_revision'] == 1
    item = snapshot['items'][0]
    assert item['location_id'] == 'cabinet-01:drawer-2'  # Ignores model's guessed drawer 1.
    assert item['stored_at'] and item['field_meta']['stored_at']['source'] == 'system'
    assert len(store.get_item(item['id'])['observations']) == 4
    worker.process(first['id'])
    assert len(calls) == 1
    assert store.get_inventory_snapshot()['inventory_revision'] == 1
    second = capture_session(sessions)
    recorder(sessions, packet('remove', existing=True)).process(second['id'])
    assert not store.get_inventory_snapshot()['items']
    third = capture_session(sessions, drawer=3)
    recorder(sessions, packet('return', existing=True)).process(third['id'])
    restored = store.get_inventory_snapshot()['items']
    assert [i['id'] for i in restored] == [item['id']]
    assert restored[0]['version'] == 3
    assert restored[0]['location_id'] == 'cabinet-01:drawer-3'
    observed_ids = {o['id'] for o in store.get_item(item['id'])['observations']}
    assert set(restored[0]['field_meta']['stored_at']['evidence_refs']) <= observed_ids
    assert len(store.history(item['id'])) == 3
    with store.db.connect() as db:
        assert db.execute('SELECT count(*) FROM storage_visits WHERE closed=0').fetchone()[0] == 1
        assert not list(db.execute('PRAGMA foreign_key_check'))


def test_missing_boundary_does_not_modify_inventory(tmp_path):
    store, sessions = services(tmp_path)
    session = capture_session(sessions)
    value = packet()
    value['observations'][0].update(region='outside', bbox=[100, 100, 400, 500])
    recorder(sessions, value).process(session['id'])
    assert sessions.get(session['id'])['state'] == 'pending'
    assert not store.get_inventory_snapshot()['items']


def test_new_baseline_is_distinct_from_insertion_and_not_duplicated(tmp_path):
    store, sessions = services(tmp_path)
    session = capture_session(sessions)
    value = packet()
    value['events'] = []
    value['observations'] = value['observations'][1:]
    recorder(sessions, value).process(session['id'])
    item = store.get_inventory_snapshot()['items'][0]
    assert item['stored_at'] is None
    assert store.receipts('cabinet-01')[0]['filmed_inventory_action'] is False
    second = capture_session(sessions)
    value = packet(oid='obj-002')
    value['events'] = []
    value['observations'] = value['observations'][1:]
    recorder(sessions, value).process(second['id'])
    assert sessions.get(second['id'])['state'] == 'pending'
    assert len(store.get_inventory_snapshot()['items']) == 1


def test_disconnect_interruption_and_restart_never_queue_incomplete_capture(tmp_path):
    store, sessions = services(tmp_path)
    capture = BridgeCapture(sessions)
    frame = {'generation': 1, 'received_ms': 0, 'received_unix_ms': round(time.time() * 1000)}
    capture.step({'cabinet_id': 'cabinet-01', 'connected': True, 'state': {
        'source': 'vision', 'boot_id': 'b', 'open': [True, False, False]}},
        {'epoch': 'e', 'frames': [frame]}, lambda _: jpeg())
    capture.step({'connected': False}, {'frames': []}, lambda _: jpeg())
    session = sessions.list()[0]
    assert session['state'] == 'pending'
    assert 'bridge_or_camera_disconnected' in session['detail']['reasons']
    other, _ = sessions.create('cabinet-01')
    sessions.recover()
    assert sessions.get(other)['state'] == 'pending'
    assert not store.get_inventory_snapshot()['items']


def test_revision_conflict_and_tampered_evidence_stop_writes(tmp_path):
    store, sessions = services(tmp_path)
    session = capture_session(sessions)
    worker = recorder(sessions, packet())
    manifest = json.loads((Path(session['root']) / 'session.json').read_text())
    worker.context(manifest, Path(session['root']))
    store.create_item('cabinet-01', {'name': '手工物品'}, 0, 'manual', 'user')
    with pytest.raises(Conflict, match='inventory_changed'):
        worker.process(session['id'])
    assert len(store.get_inventory_snapshot()['items']) == 1
    path = Path(session['root']) / manifest['frames'][0]['file']
    path.write_bytes(b'changed')
    with pytest.raises(ValueError, match='hash mismatch'):
        worker.process(session['id'])


def test_api_real_inventory_evidence_edit_and_review(tmp_path):
    app = create_app(tmp_path / 'api.sqlite3', workers=False)
    with TestClient(app) as client:
        sessions = app.state.sessions
        session = capture_session(sessions)
        recorder(sessions, packet()).process(session['id'])
        value = client.get('/api/inventory').json()
        item = value['items'][0]
        assert client.get(item['photo']['thumbnailUrl']).headers['content-type'] == 'image/jpeg'
        assert client.get(item['photo']['originalUrl']).status_code == 200
        body = {'base_revision': 1, 'expected_version': 1, 'idempotency_key': 'edit-test',
                'patch': {'name': '绿色充电器', 'expires_on': '2027-01-01'}}
        assert client.patch('/api/inventory/items/' + item['id'], json=body).status_code == 200
        assert client.patch('/api/inventory/items/' + item['id'], json=body).status_code == 200
        assert client.get('/api/inventory').json()['inventory_revision'] == 2
        assert client.patch('/api/inventory/items/' + item['id'], json={**body, 'idempotency_key': 'stale'}).status_code == 409
        assert client.post('/api/inventory/items', json={}, headers={'origin': 'https://external.example'}).status_code == 403
        assert 'root' not in client.get('/api/recording/state').text
        detail = client.get('/api/recording/sessions/' + session['id']).json()
        assert client.get(detail['frames'][0]['url']).status_code == 200
        assert client.post('/api/recording/sessions/' + session['id'] + '/retry').status_code == 409


def test_capture_outlives_ten_second_bridge_cache(tmp_path):
    _, sessions = services(tmp_path)
    session = capture_session(sessions, close_at=22)
    manifest = json.loads((Path(session['root']) / 'session.json').read_text())
    assert manifest['complete']
    assert manifest['frames'][0]['received_ms'] == 0
    assert manifest['frames'][-1]['received_ms'] == 22500
    assert len(manifest['frames']) == 91


def test_two_second_preroll_and_tail_do_not_cut_off_a_long_operation(tmp_path):
    _, sessions = services(tmp_path)
    session = capture_session(sessions, opened_at=5, close_at=205, tail_ms=2000)
    manifest = json.loads((Path(session['root']) / 'session.json').read_text())
    assert manifest['complete']
    assert manifest['capture_policy'] == {'preroll_ms': 2000, 'tail_ms': 2000}
    assert manifest['frames'][0]['received_ms'] == 3000
    assert manifest['frames'][-1]['received_ms'] == 207000


@pytest.mark.parametrize('failure', ['gap', 'unknown', 'reboot'])
def test_capture_discontinuities_preserve_evidence_without_inventory_writes(tmp_path, failure):
    store, sessions = services(tmp_path)
    now = [0.]
    capture = BridgeCapture(sessions, clock=lambda: now[0], tail_ms=0)
    frames = []
    for index in range(24):
        now[0] = index / 4
        if failure == 'gap' and 10 <= index <= 14:
            continue
        frames.append({'generation': index + 1, 'received_ms': index * 250,
                       'received_unix_ms': round(time.time() * 1000)})
        opened = [False, 2 <= now[0] < 4, False]
        known = [failure != 'unknown' or index != 10] * 3
        capture.step({'cabinet_id': 'cabinet-01', 'connected': True, 'state': {
            'source': 'vision', 'boot_id': 'new' if failure == 'reboot' and index >= 10 else 'b',
            'open': opened, 'known': known}}, {'epoch': 'e', 'frames': frames[-9:]}, lambda _: jpeg())
    assert all(s['state'] == 'pending' for s in sessions.list())
    assert not store.get_inventory_snapshot()['items']


def test_crash_after_first_commit_resumes_without_duplicate_writes(tmp_path):
    store, sessions = services(tmp_path)
    session = capture_session(sessions)
    value = packet()
    second = packet(oid='obj-002')
    second['objects'][0]['name'] = '另一件物品'
    for point in second['observations']:
        point['bbox'] = [700, 100, 1000, 700] if point['region'] == 'frame_edge' else [600, 100, 950, 800]
    for field in ('objects', 'observations', 'events'):
        value[field] += second[field]
    worker = recorder(sessions, value)
    original = worker.write_action
    calls = []

    def interrupted(*args):
        result = original(*args)
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('simulated crash after committed transaction')
        return result

    worker.write_action = interrupted
    with pytest.raises(RuntimeError):
        worker.process(session['id'])
    assert store.get_inventory_snapshot()['inventory_revision'] == 1
    worker.write_action = original
    worker.process(session['id'])
    worker.process(session['id'])
    assert store.get_inventory_snapshot()['inventory_revision'] == 2
    assert len(store.get_inventory_snapshot()['items']) == 2


def test_incomplete_session_review_cannot_force_automatic_commit(tmp_path):
    app = create_app(tmp_path / 'review.sqlite3', workers=False)
    with TestClient(app) as client:
        identity, _ = app.state.sessions.create('cabinet-01')
        app.state.sessions.update(identity, 'pending', {'reasons': ['capture_process_interrupted']})
        base = '/api/recording/sessions/' + identity
        assert client.post(base + '/retry').status_code == 409
        assert client.post(base + '/dismiss').json()['state'] == 'dismissed'
        assert not client.get('/api/inventory').json()['items']
