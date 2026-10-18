import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from app.agent_server import create_app
from app.api_keys import AccessError, KeyStore, future
from app.connections import create_owner_app
from app.database import Database, new_id, utcnow
from app.store import Store
from test_database import make_run, plan_for


@pytest.fixture
def services(tmp_path):
    store = Store(tmp_path / 'inventory.sqlite3')
    store.initialize()
    store.initialize('cabinet-02', 'Second cabinet')
    return store, KeyStore(store.db), TestClient(create_app(store.db.path))


def credential(keys, scopes=('inventory:read',), cabinets=('cabinet-01',)):
    return keys.create('Personal Agent', cabinets, scopes)


def auth(result):
    return {'Authorization': 'Bearer ' + result['key']}


def add(store, name='白色充电线', cabinet='cabinet-01', key=None):
    snapshot = store.get_inventory_snapshot(cabinet)
    store.create_item(cabinet, {'name': name, 'category': '线材', 'location_id': cabinet + ':drawer-3'},
                      snapshot['inventory_revision'], key or new_id(), 'user:test')
    return store.get_inventory_snapshot(cabinet)['items'][-1]


def test_keys_persist_hash_only_expire_revoke_and_rotate(services):
    store, keys, client = services
    first = credential(keys)
    assert KeyStore(store.db.path).authenticate(first['key']).cabinets == ('cabinet-01',)
    assert 'key' not in keys.list()[0]
    with store.db.connect() as db:
        assert first['key'].split('_', 2)[2] not in json.dumps([dict(r) for r in db.execute('SELECT * FROM api_keys')])
    replacement = keys.rotate(first['connection']['id'])
    assert client.get('/v1/cabinets', headers=auth(replacement)).status_code == 200
    assert client.get('/v1/cabinets', headers=auth(first)).status_code == 200  # Explicit grace period.
    keys.revoke(first['connection']['id'])
    assert client.get('/v1/cabinets', headers=auth(first)).status_code == 401
    with store.db.transaction() as db:
        db.execute('UPDATE api_keys SET expires_at=? WHERE id=?', (future(days=-1), replacement['connection']['id']))
    assert client.get('/v1/cabinets', headers=auth(replacement)).status_code == 401


def test_authentication_scope_and_cross_cabinet_access(services):
    store, keys, client = services
    allowed = add(store)
    other = add(store, cabinet='cabinet-02')
    key = credential(keys)
    assert client.get('/v1/cabinets').status_code == 401
    assert client.get('/v1/cabinets', headers={'Authorization': 'Bearer forged'}).status_code == 401
    assert client.get('/v1/cabinets', headers={'Authorization': 'Bearer ' + key['key'] + 'changed'}).status_code == 401
    cabinets = client.get('/v1/cabinets', headers=auth(key)).json()['cabinets']
    assert [c['id'] for c in cabinets] == ['cabinet-01']
    assert len(cabinets[0]['locations']) == 3
    assert client.get('/v1/cabinets/cabinet-02/inventory', headers=auth(key)).status_code == 404
    assert client.get('/v1/items/' + other['id'], headers=auth(key)).status_code == 404
    assert client.get('/v1/items/' + other['id'] + '/history', headers=auth(key)).status_code == 404
    assert client.get('/v1/items/' + allowed['id'] + '/history', headers=auth(key)).status_code == 403
    assert client.get('/v1/items/' + allowed['id'] + '/images/fake', headers=auth(key)).status_code == 403


def test_snapshot_filters_pagination_revision_and_unknown_values(services):
    store, keys, client = services
    add(store, '充电线一')
    add(store, '充电线二')
    key = credential(keys)
    url = '/v1/cabinets/cabinet-01/inventory'
    response = client.get(url, params={'limit': 1}, headers=auth(key))
    snapshot = response.json()
    assert response.headers['Cache-Control'] == 'no-store'
    assert snapshot['inventory_known'] is False
    assert snapshot['completeness'] == 'unknown_baseline' and snapshot['source_live_status'] == 'unknown'
    assert snapshot['items'][0]['stored_at'] is None and snapshot['items'][0]['quantity'] is None
    assert snapshot['items'][0]['last_observed_at'] is None
    assert snapshot['items'][0]['location']['id'] == 'cabinet-01:drawer-3'
    assert snapshot['items'][0]['images'] is None
    page2 = client.get(url, params={'limit': 1, 'cursor': snapshot['next_cursor']}, headers=auth(key)).json()
    assert page2['items'][0]['id'] != snapshot['items'][0]['id'] and page2['next_cursor'] is None
    assert client.get(url, params={'q': '不存在'}, headers=auth(key)).json()['items'] == []
    assert len(client.get(url, params={'q': '充电线', 'location_id': 'cabinet-01:drawer-3'}, headers=auth(key)).json()['items']) == 2
    assert client.get(url, params={'limit': 101}, headers=auth(key)).status_code == 422
    assert client.get(url, params={'presence_status': 'fiction'}, headers=auth(key)).status_code == 422
    assert client.get(url, params={'cursor': 'invalid'}, headers=auth(key)).status_code == 422
    assert client.get(url, params={'q': 'different', 'limit': 1, 'cursor': snapshot['next_cursor']}, headers=auth(key)).status_code == 422
    add(store, '充电线三')
    assert client.get(url, params={'limit': 1, 'cursor': snapshot['next_cursor']}, headers=auth(key)).status_code == 409


def test_removed_is_explicit_and_unseen_is_not_removed(services):
    store, keys, client = services
    item = add(store)
    key = credential(keys)
    url = '/v1/cabinets/cabinet-01/inventory'
    store.correct_item('cabinet-01', item['id'], {'presence_status': 'unseen'}, 1, 1, new_id(), 'user:test')
    assert client.get(url, headers=auth(key)).json()['items'][0]['presence_status'] == 'unseen'
    store.correct_item('cabinet-01', item['id'], {'presence_status': 'removed'}, 2, 2, new_id(), 'user:test')
    assert not client.get(url, headers=auth(key)).json()['items']
    assert len(client.get(url, params={'include_removed': True}, headers=auth(key)).json()['items']) == 1
    assert len(client.get(url, params={'presence_status': 'removed'}, headers=auth(key)).json()['items']) == 1


def test_history_and_images_are_related_scoped_and_sanitized(services, tmp_path):
    store, keys, client = services
    saved = store.import_run(make_run(tmp_path / 'run'))
    store.commit_inventory(plan_for(saved))
    item = store.get_inventory_snapshot()['items'][0]
    key = credential(keys, ('inventory:read', 'images:read', 'history:read'))
    detail = client.get('/v1/items/' + item['id'], headers=auth(key)).json()['item']
    assert 'observations' not in detail and 'notes' not in detail
    image = detail['images'][0]
    response = client.get(image['url'], headers=auth(key))
    assert response.status_code == 200 and response.headers['content-type'] == 'image/jpeg'
    foreign = add(store)
    assert client.get(f'/v1/items/{foreign["id"]}/images/{image["id"]}', headers=auth(key)).status_code == 404
    history = client.get('/v1/items/' + item['id'] + '/history', headers=auth(key)).json()['history'][0]
    assert history['filmed_inventory_action'] and history['events'][0]['media_window_ms'] == [0, 1000]
    assert 'asset_path' not in json.dumps(detail) and 'metadata_json' not in json.dumps(history)
    manual_history = client.get('/v1/items/' + foreign['id'] + '/history', headers=auth(key)).json()['history'][0]
    assert manual_history['source'] == 'manual' and not manual_history['filmed_inventory_action']


def test_image_path_escape_and_frame_access_are_rejected(services, tmp_path):
    store, keys, client = services
    saved = store.import_run(make_run(tmp_path / 'run'))
    store.commit_inventory(plan_for(saved))
    item = store.get_inventory_snapshot()['items'][0]
    key = credential(keys, ('inventory:read', 'images:read'))
    with store.db.connect() as db:
        crop = db.execute("SELECT id FROM observations WHERE kind='crop'").fetchone()[0]
        frame = db.execute("SELECT id FROM observations WHERE kind='frame'").fetchone()[0]
    assert client.get(f'/v1/items/{item["id"]}/images/{frame}', headers=auth(key)).status_code == 404
    with store.db.transaction() as db:
        db.execute('UPDATE observations SET asset_path=? WHERE id=?', ('../private.jpg', crop))
    assert client.get(f'/v1/items/{item["id"]}/images/{crop}', headers=auth(key)).status_code == 404


def test_api_is_read_only_openapi_and_audit_redaction(services):
    store, keys, client = services
    key = credential(keys)
    response = client.get('/v1/cabinets/cabinet-01/inventory?q=private-search', headers=auth(key))
    assert response.status_code == 200
    assert client.post('/api/inventory/items', json={}, headers=auth(key)).status_code == 404
    assert client.get('/api/recording/state', headers=auth(key)).status_code == 404
    assert client.post('/api/connections/keys', json={}, headers=auth(key)).status_code == 404
    schema = client.get('/openapi.json').json()
    assert schema['components']['securitySchemes']['MindBoxKey']['scheme'] == 'bearer'
    assert schema['paths']['/v1/cabinets']['get']['security'] == [{'MindBoxKey': []}]
    with store.db.connect() as db:
        logs = json.dumps([dict(r) for r in db.execute('SELECT * FROM api_access_logs')])
    assert 'private-search' not in logs and key['key'] not in logs
    assert keys.list()[0]['last_used_at']
    assert store.get_inventory_snapshot()['inventory_revision'] == 0


def test_rate_limit_and_database_failure_do_not_look_like_empty_inventory(services, monkeypatch):
    store, keys, client = services
    key = credential(keys)
    for _ in range(120):
        assert client.get('/v1/cabinets', headers=auth(key)).status_code == 200
    assert client.get('/v1/cabinets', headers=auth(key)).status_code == 429
    fresh = credential(keys)
    def broken(*args, **kwargs):
        raise sqlite3.OperationalError('private path and internal details')
    monkeypatch.setattr(client.app.state.queries, 'inventory', broken)
    response = client.get('/v1/cabinets/cabinet-01/inventory', headers=auth(fresh))
    assert response.status_code == 503 and 'items' not in response.json()
    assert 'private path' not in response.text


def test_owner_pairing_cookie_csrf_and_ordinary_key_cannot_manage(services):
    store, keys, _ = services
    owner = TestClient(create_owner_app(store.db), base_url='http://127.0.0.1', client=('127.0.0.1', 5000))
    headers = {'X-Mindbox-Request': '1', 'Origin': 'http://127.0.0.1:5180'}
    assert owner.get('/state').status_code == 403
    assert owner.get('/state', headers=headers).status_code == 401
    ordinary = credential(keys)
    assert owner.post('/keys', json={}, headers={**headers, **auth(ordinary)}).status_code == 401
    code = keys.pairing_code()
    paired = owner.post('/pair', json={'code': code}, headers=headers)
    assert paired.status_code == 200
    assert 'HttpOnly' in paired.headers['set-cookie'] and 'SameSite=strict' in paired.headers['set-cookie']
    assert owner.post('/pair', json={'code': code}, headers=headers).status_code == 401
    # Simulate mounting beneath the production cookie path.
    owner.cookies.clear()
    session = keys.pair(keys.pairing_code())
    owner.cookies.set('mindbox_owner', session)
    assert owner.get('/state', headers=headers).status_code == 200
    assert owner.get('/state', headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
    assert owner.post('/keys', headers=headers, json={'name': 'Muse', 'cabinet_ids': ['cabinet-01']}).status_code == 201
    assert owner.get('/state', headers=headers).json()['connections'][0]['name'] == 'Muse'
    keys.logout(session)
    assert owner.get('/state', headers=headers).status_code == 401


def test_owner_pairing_expiry_and_no_remote_host(services):
    store, keys, _ = services
    code = keys.pairing_code()
    with store.db.transaction() as db:
        db.execute('UPDATE api_owner_tokens SET expires_at=?', (future(minutes=-1),))
    with pytest.raises(AccessError):
        keys.pair(code)
    remote = TestClient(create_owner_app(store.db), base_url='http://evil.example', client=('127.0.0.1', 5000))
    assert remote.get('/state', headers={'X-Mindbox-Request': '1'}).status_code == 403


def test_owner_can_issue_local_pairing_code_only_with_csrf_header(services):
    store, _, _ = services
    owner = TestClient(create_owner_app(store.db), base_url='http://127.0.0.1', client=('127.0.0.1', 5000))
    headers = {'X-Mindbox-Request': '1', 'Origin': 'http://127.0.0.1:5180'}
    response = owner.post('/pairing-code', headers=headers)
    assert response.status_code == 200 and len(response.json()['code']) >= 40
    assert owner.post('/pairing-code', headers={'Origin': headers['Origin']}).status_code == 403
