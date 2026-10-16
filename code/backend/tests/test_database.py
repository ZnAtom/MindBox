import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3

from PIL import Image
import pytest

from app.database import Conflict, Database, DatabaseError, NotFound
from app.inventory_schemas import ItemValues
from app.review import inspect_run
from app.store import Store


@pytest.fixture
def store(tmp_path):
    service = Store(tmp_path / 'db/mind-box.sqlite3')
    service.initialize()
    return service


def make_run(root, changes=('add',), pending=False, events=None):
    root.mkdir(parents=True)
    image = root / 'frame.jpg'
    Image.new('RGB', (20, 20), 'green').save(image)
    frame = lambda identity, ms: {'id': identity, 'file': 'frame.jpg', 'media_time_ms': ms, 'pts': ms}
    input_hash = hashlib.sha256(str(root).encode()).hexdigest()
    media = {'input_sha256': input_hash, 'frames': [frame('f0', 0), frame('f1', 1000)], 'time_base': '1/1000'}
    items, evidence, net = [], {}, []
    for index, change in enumerate(changes):
        cid, ref = f'c{index}', f'crop{index}'
        evidence[ref] = {'file': 'frame.jpg', 'frame_id': 'f1', 'media_time_ms': 1000}
        items.append({'candidate_id': cid, 'name': '苹果', 'category': '食品', 'quantity': None,
                      'evidence_refs': [ref], 'field_meta': {}})
        net.append({'candidate_id': cid, 'change': change})
    if events is None:
        events = [{'kind': change, 'candidate_ids': [f'c{i}'], 'before_frame_id': 'f0', 'after_frame_id': 'f1',
                   'event_window_ms': [0, 1000], 'committed': False}
                  for i, change in enumerate(changes) if change in ('add', 'remove')]
    result = {'video': 'sample.mp4', 'input_sha256': input_hash, 'status': 'completed', 'items': items,
              'evidence': evidence, 'event_candidates': events, 'operation_analysis': {
                  'status': 'pending' if pending else 'consistent', 'event_candidates': events,
                  'net_changes': net, 'pending': [{'reason': 'identity_uncertain'}] if pending else []}}
    (root / 'media.json').write_text(json.dumps(media), encoding='utf-8')
    (root / 'result.json').write_text(json.dumps(result), encoding='utf-8')
    return root


def plan_for(saved, changes=('add',), key='commit-1'):
    return {'operation_id': saved['operation_id'], 'run_id': saved['run_id'],
            'base_revision': saved['base_revision'], 'idempotency_key': key, 'reviewed_by': 'test-reviewer',
            'decisions': [{'candidate_id': f'c{i}', 'action': change, 'evidence_refs': [f'crop{i}']}
                          for i, change in enumerate(changes)]}


def test_migrations_idempotent_and_foreign_keys_enforced(store):
    before = store.db.status()
    assert store.db.migrate()['migrations'] == before['migrations']
    assert before['journal_mode'] == 'wal' and before['integrity'] == 'ok'
    assert before['foreign_key_errors'] == []
    with pytest.raises(sqlite3.IntegrityError), store.db.transaction() as connection:
        connection.execute("INSERT INTO locations VALUES ('bad', 'missing', 'bad')")


def test_reading_missing_database_does_not_create_a_file(tmp_path):
    database = Database(tmp_path / 'missing.sqlite3')
    with pytest.raises(NotFound):
        database.status()
    assert not database.path.exists()


def test_migration_checksum_tamper_is_detected(store):
    with store.db.transaction() as connection:
        connection.execute("UPDATE schema_migrations SET sha256 = 'modified'")
    with pytest.raises(DatabaseError, match='checksum'):
        store.db.migrate()


def test_unknown_inventory_is_not_empty(store, tmp_path):
    assert store.get_inventory_snapshot()['inventory_known'] is False
    with pytest.raises(Conflict):
        store.initialize(known_empty=True)
    known = Store(tmp_path / 'known.sqlite3')
    assert known.initialize(known_empty=True)['inventory_known'] is True


def test_device_identity_deduplication_and_weight_disabled(store):
    store.register_device('esp', 'cabinet-01', 'sensor')
    event = {'event_id': 'e1', 'device_id': 'esp', 'boot_id': 'b1', 'seq': 1,
             'kind': 'door.opened', 'payload': {'drawer': 1}}
    assert not store.ingest_event('esp', event)['duplicate']
    assert store.ingest_event('esp', event)['duplicate']
    with pytest.raises(Conflict):
        store.ingest_event('esp', {**event, 'kind': 'door.closed'})
    with pytest.raises(DatabaseError):
        store.ingest_event('imposter', event)
    with pytest.raises(DatabaseError, match='Weight'):
        store.ingest_event('esp', {**event, 'kind': 'weight.stable'})
    assert not store.ingest_event('esp', {**event, 'event_id': 'e2', 'boot_id': 'b2'})['duplicate']


def test_import_deduplicates_archives_and_does_not_create_inventory(store, tmp_path):
    root = make_run(tmp_path / 'run')
    saved = store.import_run(root)
    assert saved['observation_count'] == 3
    assert store.import_run(root)['run_id'] == saved['run_id']
    assert store.get_inventory_snapshot()['items'] == []
    (root / 'frame.jpg').unlink()
    with store.db.connect() as connection:
        for row in connection.execute('SELECT * FROM observations'):
            asset = store.db.path.parent / row['asset_path']
            assert asset.exists() and hashlib.sha256(asset.read_bytes()).hexdigest() == row['sha256']


def test_changed_run_same_identity_and_path_escape_rejected(store, tmp_path):
    root = make_run(tmp_path / 'run')
    store.import_run(root)
    result = json.loads((root / 'result.json').read_text())
    result['items'][0]['name'] = 'different'
    (root / 'result.json').write_text(json.dumps(result))
    with pytest.raises(Conflict):
        store.import_run(root)
    result['evidence']['crop0']['file'] = '../secret.jpg'
    (tmp_path / 'secret.jpg').write_bytes(b'private')
    (root / 'result.json').write_text(json.dumps(result))
    with pytest.raises(DatabaseError, match='inside'):
        store.import_run(root)


def test_tool_requested_extra_frames_are_archived(store, tmp_path):
    root = make_run(tmp_path / 'run')
    result = json.loads((root / 'result.json').read_text())
    for events in (result['event_candidates'], result['operation_analysis']['event_candidates']):
        events[0]['after_frame_id'] = 'extra-frame'
    (root / 'result.json').write_text(json.dumps(result))
    (root / 'action-tool-trace.json').write_text(json.dumps({'trace': [{'tool': 'get_frames', 'result': {
        'status': 'ok', 'frames': [{'id': 'extra-frame', 'file': 'frame.jpg', 'media_time_ms': 800, 'pts': 800}]}}]}))
    saved = store.import_run(root)
    assert saved['observation_count'] == 4
    assert store.commit_inventory(plan_for(saved))['status'] == 'committed'


def test_failed_preparation_is_saved_as_failed_not_a_ready_operation(store, tmp_path):
    root = tmp_path / 'failed'
    root.mkdir()
    (root / 'result.json').write_text(json.dumps({'video': 'failed.mp4', 'input_sha256': 'b' * 64,
        'status': 'failed', 'items': [], 'errors': ['decode failed']}))
    saved = store.import_run(root)
    assert store.get_operation(saved['operation_id'])['state'] == 'failed'
    with pytest.raises(Conflict):
        store.resolve_pending(saved['run_id'], 'ignore', 'reviewer')


def test_commit_real_receipt_retry_and_same_key_conflict(store, tmp_path):
    saved = store.import_run(make_run(tmp_path / 'run'))
    plan = plan_for(saved)
    receipt = store.commit_inventory(plan)
    assert receipt['revision'] == 1 and len(receipt['added_item_ids']) == 1
    assert store.commit_inventory(plan) == receipt
    snapshot = Store(store.db.path).get_inventory_snapshot()
    assert snapshot['inventory_revision'] == 1 and len(snapshot['items']) == 1
    assert snapshot['items'][0]['stored_at'] is None
    assert snapshot['items'][0]['quantity'] is None
    changed = copy.deepcopy(plan)
    changed['reviewed_by'] = 'other'
    with pytest.raises(Conflict):
        store.commit_inventory(changed)
    with pytest.raises(Conflict):
        store.commit_inventory({**plan, 'idempotency_key': 'second-commit'})
    assert len(store.receipts('cabinet-01')) == 1


def test_failed_second_change_rolls_back_everything(store, tmp_path):
    saved = store.import_run(make_run(tmp_path / 'run', ('add', 'add')))
    plan = plan_for(saved, ('add', 'add'))
    plan['decisions'][1]['evidence_refs'] = ['unknown']
    with pytest.raises(DatabaseError):
        store.commit_inventory(plan)
    assert store.get_inventory_snapshot()['inventory_revision'] == 0
    with store.db.connect() as connection:
        for table in ('items', 'inventory_commits', 'inventory_changes', 'storage_visits', 'jobs'):
            assert connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0
    assert store.get_operation(saved['operation_id'])['state'] == 'ready'


def test_pending_does_not_commit_until_reviewed(store, tmp_path):
    saved = store.import_run(make_run(tmp_path / 'run', pending=True))
    with pytest.raises(Conflict):
        store.commit_inventory(plan_for(saved))
    store.resolve_pending(saved['run_id'], 'Checked physical identity and action', 'operator')
    assert store.commit_inventory(plan_for(saved))['status'] == 'committed'


def test_old_inventory_revision_and_old_attempt_rejected(store, tmp_path):
    root = make_run(tmp_path / 'run')
    saved = store.import_run(root)
    store.create_item('cabinet-01', {'name': '已有物品'}, 0, 'seed', 'operator')
    with pytest.raises(Conflict, match='revision'):
        store.commit_inventory(plan_for(saved))
    newer = store.import_run(root, operation_id=saved['operation_id'], source_key='new-attempt', base_revision=1)
    with pytest.raises(Conflict, match='not current'):
        store.commit_inventory(plan_for(saved))
    assert store.commit_inventory(plan_for(newer))['revision'] == 2


def test_candidate_ids_from_different_runs_do_not_merge(store, tmp_path):
    for index in range(2):
        saved = store.import_run(make_run(tmp_path / f'run-{index}'))
        store.commit_inventory(plan_for(saved, key=f'key-{index}'))
    items = store.get_inventory_snapshot()['items']
    assert len(items) == 2 and items[0]['id'] != items[1]['id']


def test_same_video_reanalyzed_in_new_directory_cannot_duplicate_commit(store, tmp_path):
    import shutil
    root = make_run(tmp_path / 'first')
    first = store.import_run(root)
    shutil.copytree(root, tmp_path / 'second')
    second = store.import_run(tmp_path / 'second')
    assert first['operation_id'] == second['operation_id']
    with pytest.raises(Conflict, match='not current'):
        store.commit_inventory(plan_for(first))
    store.commit_inventory(plan_for(second))
    shutil.copytree(root, tmp_path / 'third')
    with pytest.raises(Conflict, match='immutable'):
        store.import_run(tmp_path / 'third')


def test_unanchored_model_time_cannot_become_stored_at(store, tmp_path):
    root = make_run(tmp_path / 'run')
    result = json.loads((root / 'result.json').read_text())
    result['items'][0]['stored_at'] = '2026-10-16T10:00:00Z'
    (root / 'result.json').write_text(json.dumps(result))
    saved = store.import_run(root)
    with pytest.raises(DatabaseError, match='clock anchor'):
        store.commit_inventory(plan_for(saved))
    assert store.get_inventory_snapshot()['inventory_revision'] == 0


def test_remove_return_reuses_item_identity_and_keeps_visits(store, tmp_path):
    receipt = store.create_item('cabinet-01', {'name': '苹果'}, 0, 'seed', 'operator')
    item_id = receipt['added_item_ids'][0]
    for index, (net, action) in enumerate((('remove', 'remove'), ('add', 'return'))):
        saved = store.import_run(make_run(tmp_path / f'run-{index}', (net,)))
        plan = plan_for(saved, (action,), f'operation-{index}')
        plan['decisions'][0].update(item_id=item_id, expected_version=index + 1)
        store.commit_inventory(plan)
    item = store.get_item(item_id)
    assert item['presence_status'] == 'present' and item['version'] == 3
    assert len(item['storage_visits']) == 2
    assert sum(v['closed'] == 0 for v in item['storage_visits']) == 1
    assert len(store.history(item_id)) == 3


def test_take_and_return_keeps_net_count_but_saves_intermediate_history(store, tmp_path):
    receipt = store.create_item('cabinet-01', {'name': '苹果'}, 0, 'seed', 'operator')
    item_id = receipt['added_item_ids'][0]
    events = [{'kind': kind, 'candidate_ids': ['c0'], 'before_frame_id': 'f0', 'after_frame_id': 'f1',
               'event_window_ms': [i * 400, i * 400 + 300], 'committed': False} for i, kind in enumerate(('remove', 'return'))]
    saved = store.import_run(make_run(tmp_path / 'run', ('kept',), events=events))
    plan = plan_for(saved, ('keep',))
    plan['decisions'][0].update(item_id=item_id, expected_version=1)
    store.commit_inventory(plan)
    item = store.get_item(item_id)
    assert len(store.get_inventory_snapshot()['items']) == 1
    assert len(item['storage_visits']) == 2
    with store.db.connect() as connection:
        assert connection.execute('SELECT COUNT(*) FROM verified_events').fetchone()[0] == 2


def test_cross_cabinet_item_and_location_rejected(store, tmp_path):
    store.initialize('other')
    receipt = store.create_item('other', {'name': 'other'}, 0, 'seed', 'operator')
    saved = store.import_run(make_run(tmp_path / 'run', ('remove',)))
    plan = plan_for(saved, ('remove',))
    plan['decisions'][0].update(item_id=receipt['added_item_ids'][0], expected_version=1)
    with pytest.raises(Conflict):
        store.commit_inventory(plan)
    with pytest.raises(DatabaseError):
        store.create_item('cabinet-01', {'name': 'bad', 'location_id': 'other:drawer-1'}, 0, 'bad', 'operator')
    assert store.get_inventory_snapshot()['inventory_revision'] == 0


def test_correction_soft_removal_date_source_and_version(store):
    receipt = store.create_item('cabinet-01', {'name': '苹果'}, 0, 'seed', 'operator')
    item_id = receipt['added_item_ids'][0]
    updated = store.correct_item('cabinet-01', item_id, {'expires_on': '2026-11-01', 'name': '青苹果'},
                                 1, 1, 'correction', 'operator')
    assert store.get_item(item_id)['field_meta']['expires_on']['source'] == 'user'
    assert store.correct_item('cabinet-01', item_id, {'expires_on': '2026-11-01', 'name': '青苹果'},
                               1, 1, 'correction', 'operator') == updated
    with pytest.raises(Conflict):
        store.correct_item('cabinet-01', item_id, {'name': 'stale'}, 1, 2, 'bad', 'operator')
    store.correct_item('cabinet-01', item_id, {'presence_status': 'removed'}, 2, 2, 'remove', 'operator')
    assert store.get_inventory_snapshot()['items'] == []
    assert len(store.get_inventory_snapshot(include_removed=True)['items']) == 1
    with pytest.raises(ValueError):
        ItemValues(name='unsafe', expires_on='2026-12-01', field_meta={
            'expires_on': {'source': 'vision', 'certainty': 'estimated'}})


def test_optimistic_concurrent_writes_have_one_winner(store):
    def write(index):
        try:
            store.create_item('cabinet-01', {'name': str(index)}, 0, f'key-{index}', 'operator')
            return 'committed'
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write, range(2)))
    assert sorted(outcomes) == ['committed', 'conflict']
    assert store.get_inventory_snapshot()['inventory_revision'] == 1


def test_backup_restores_inventory_and_evidence(store, tmp_path):
    saved = store.import_run(make_run(tmp_path / 'run'))
    store.commit_inventory(plan_for(saved))
    backup = store.db.backup(tmp_path / 'backup')
    restored = Store(backup['database'])
    assert restored.get_inventory_snapshot() == store.get_inventory_snapshot()
    item = restored.get_item(store.receipts('cabinet-01')[0]['added_item_ids'][0])
    for observation in item['observations']:
        assert (restored.db.path.parent / observation['asset_path']).exists()
    assert restored.db.status()['integrity'] == 'ok'


def test_inspection_only_reports_real_receipts(store, tmp_path):
    root = make_run(tmp_path / 'run/sample')
    saved = store.import_run(root)
    result = json.loads((root / 'result.json').read_text())
    (root.parent / 'summary.json').write_text(json.dumps([result]))
    before = inspect_run(root.parent, database=store.db.path)
    assert before['database']['inventory_commits'] == 0
    receipt = store.commit_inventory(plan_for(saved))
    after = inspect_run(root.parent, database=store.db.path)
    assert after['per_video'][0]['inventory_commit_receipts'] == [receipt]
