from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import wave

from fastapi.testclient import TestClient
import pytest

from app.capture import Sessions
from app.client import write_json
from app.config import Settings
from app.database import DatabaseError
from app.live_tasks import LiveTasks
from app.server import create_app
from app.store import Store
from app.voice_api import VoiceAssistant
from app.voice_runtime import VoiceRuntime


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / 'inventory.sqlite3')
    store.initialize(known_empty=True)
    store.create_item('cabinet-01', {'name': '坚果', 'category': '食品',
        'location_id': 'cabinet-01:drawer-1'}, 0, 'nuts', 'test')
    return store


def facts(**values):
    return {'subject_raw': '坚果', 'production_raw': None, 'expiry_raw': None,
            'shelf_life_raw': None, **values}


def test_live_fact_writes_reuse_speech_provenance_and_date_derivation(store):
    tasks = LiveTasks(store)
    result = tasks.record_facts(facts(production_raw='2026年10月1日', shelf_life_raw='保质期六个月'),
        '坚果生产日期是2026年10月1日，保质期六个月', 'cabinet-01', 'interaction-1')
    assert result['status'] == 'applied'
    item = store.get_inventory_snapshot()['items'][0]
    assert item['production_on'] == '2026-10-01'
    assert item['expires_on'] == '2027-04-01'
    assert item['field_meta']['expires_on']['source'] == 'derived'
    assert item['field_meta']['expires_on']['evidence_refs'][0].startswith('speech:live-')
    revision = store.get_inventory_snapshot()['inventory_revision']
    tasks.record_facts(facts(production_raw='2026年10月1日', shelf_life_raw='保质期六个月'),
        '坚果生产日期是2026年10月1日，保质期六个月', 'cabinet-01', 'interaction-1')
    assert store.get_inventory_snapshot()['inventory_revision'] == revision


def test_missing_year_conflicting_facts_and_duplicate_names_do_not_guess(store):
    tasks = LiveTasks(store)
    assert tasks.record_facts(facts(expiry_raw='12月1日'), '坚果12月1日到期',
                              'cabinet-01', 'no-year')['status'] == 'needs_input'
    assert store.get_inventory_snapshot()['items'][0]['expires_on'] is None
    tasks.record_facts(facts(expiry_raw='2027年12月1日'), '坚果2027年12月1日到期', 'cabinet-01', 'dated')
    result = tasks.record_facts(facts(expiry_raw='2028年12月1日'), '坚果2028年12月1日到期', 'cabinet-01', 'conflict')
    assert result['status'] == 'pending'
    assert store.get_inventory_snapshot()['items'][0]['expires_on'] == '2027-12-01'
    snapshot = store.get_inventory_snapshot()
    store.create_item('cabinet-01', {'name': '坚果', 'location_id': 'cabinet-01:drawer-2'},
                      snapshot['inventory_revision'], 'nuts-2', 'test')
    result = tasks.record_facts(facts(expiry_raw='2029年12月1日'), '坚果2029年12月1日到期', 'cabinet-01', 'ambiguous')
    assert result['status'] == 'needs_input'
    assert len(result['candidates']) == 2
    with pytest.raises(DatabaseError, match='quote'):
        tasks.record_facts(facts(expiry_raw='2030年12月1日'), '坚果2029年12月1日到期', 'cabinet-01', 'invented')


def test_door_speech_prefers_this_operation_over_an_existing_same_type_item(store):
    store.create_item('cabinet-01', {'name': '白色袋装食品', 'category': '食品',
        'location_id': 'cabinet-01:drawer-2'}, 1, 'new-food', 'test')
    items = store.get_inventory_snapshot()['items']
    new_item = next(i for i in items if i['name'] == '白色袋装食品')
    result = LiveTasks(store).record_facts(facts(expiry_raw='2027年12月1日'), '坚果2027年12月1日到期',
        'cabinet-01', 'door-new-nuts', scope=[new_item['id']])
    assert result['item_id'] == new_item['id']
    assert next(i for i in store.get_inventory_snapshot()['items'] if i['name'] == '坚果')['expires_on'] is None


def test_confirming_a_manual_date_keeps_user_provenance_and_can_add_shelf_life(store):
    item = store.get_inventory_snapshot()['items'][0]
    store.correct_item('cabinet-01', item['id'], {'expires_on': '2027-12-01'}, 1, 1, 'manual-date', 'user')
    result = LiveTasks(store).record_facts(facts(expiry_raw='2027年12月1日', shelf_life_raw='保质期六个月'),
        '坚果2027年12月1日到期，保质期六个月', 'cabinet-01', 'confirm-date')
    assert result['status'] == 'applied'
    item = store.get_inventory_snapshot()['items'][0]
    assert item['field_meta']['expires_on']['source'] == 'user'
    assert item['shelf_life_raw'] == '保质期六个月'


def test_reminder_due_delivery_receipts_and_daily_next_occurrence(store):
    tasks = LiveTasks(store)
    due = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    created = tasks.schedule('cabinet-01', '吃坚果', due, 'daily-nuts', 'daily')
    assert created['status'] == 'scheduled'
    tasks.tick(due)
    deliveries = tasks.deliveries('cabinet-01')
    assert {entry['channel'] for entry in deliveries} == {'voice', 'web'}
    for entry in deliveries:
        kind = 'played' if entry['channel'] == 'voice' else 'received'
        assert tasks.acknowledge(entry['id'], entry['channel'], kind, 'cabinet-01')['state'] == kind
    assert tasks.deliveries('cabinet-01') == []
    pending = [r for r in tasks.reminders('cabinet-01') if r['status'] == 'pending']
    assert len(pending) == 1 and pending[0]['repeat'] == 'daily'
    assert datetime.fromisoformat(pending[0]['due_at']) - datetime.fromisoformat(due) == timedelta(days=1)
    tasks.tick(due)
    assert len([r for r in tasks.reminders('cabinet-01') if r['status'] == 'pending']) == 1
    tasks.cancel(pending[0]['id'], 'cabinet-01')
    with store.db.connect() as db:
        assert db.execute('SELECT enabled FROM live_reminder_series').fetchone()[0] == 0
        assert set(row[0] for row in db.execute("SELECT state FROM jobs WHERE kind='reminder.deliver'")) == {'done'}
        assert not list(db.execute('PRAGMA foreign_key_check'))


class ToolClient:
    def __init__(self, actions):
        self.actions = list(actions)

    def safe(self, error):
        return str(error)

    def chat(self, messages, stage, **kwargs):
        if self.actions:
            name, arguments = self.actions.pop(0)
            return {'choices': [{'message': {'content': None, 'tool_calls': [{
                'id': 'call-' + str(len(self.actions)), 'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}}]}
        return {'choices': [{'message': {'content': '已保存坚果的到期日，并设置提醒。'}}]}


def assistant(store, actions):
    return VoiceAssistant(database=store.db.path, settings=Settings('https://unused.invalid', 'test', 'fake'),
                          client=ToolClient(actions))


def test_text_uses_same_tool_pipeline_to_write_facts_and_a_real_reminder(store):
    due = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    service = assistant(store, [
        ('record_item_facts', facts(expiry_raw='2027年12月1日')),
        ('schedule_reminder', {'message': '吃坚果', 'due_at': due, 'repeat': 'once',
            'item_query': '坚果', 'evidence': '十分钟后提醒我吃坚果'}),
    ])
    result = service.text_query('坚果2027年12月1日到期，十分钟后提醒我吃坚果')
    assert len(result['operations']) == 2
    assert result['operations'][0]['result']['status'] == 'applied'
    assert result['operations'][1]['result']['status'] == 'scheduled'
    assert store.get_inventory_snapshot()['items'][0]['expires_on'] == '2027-12-01'
    reminders = service.tasks.reminders('cabinet-01')
    assert reminders[0]['item_id'] == store.get_inventory_snapshot()['items'][0]['id']
    with store.db.connect() as db:
        assert db.execute('SELECT origin,state FROM voice_interactions').fetchone()[:] == ('text', 'completed')


def test_plain_text_location_query_reads_sqlite_without_remote_ai(store):
    service = VoiceAssistant(database=store.db.path,
        settings=Settings('https://unused.invalid', '', 'unused'), client=ToolClient([]))
    result = service.text_query('坚果在哪里？')
    assert result['answer'] == '坚果在第一层。'
    assert result['inventory']['items'][0]['name'] == '坚果'


def test_audio_waits_for_its_video_commit_then_resolves_this_item(store, tmp_path):
    sessions = Sessions(store, tmp_path / 'sessions')
    identity, root = sessions.create('cabinet-01')
    write_json(root / 'session.json', {'id': identity, 'boot_id': 'board', 'trigger_monotonic': 100,
        'frames': [{'received_unix_ms': 1000000}]})
    write_json(root / 'audio.json', {'stream_started_unix_ms': 1002000})
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as output:
        output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\0\0' * 1600)
    service = assistant(store, [('record_item_facts', facts(subject_raw='这个', expiry_raw='2027年12月1日'))])
    class Transcriber:
        def transcribe(self, payload):
            return {'text': '这个2027年12月1日到期', 'segments': []}
    service.transcriber = Transcriber()
    runtime = VoiceRuntime(service, sessions, 'http://unused.invalid')
    metadata = {'complete': True, 'session_id': 'audio-test', 'boot_id': 'board',
        'cabinet_id': 'cabinet-01', 'trigger_received_monotonic': 100.05, 'stream_started_unix_ms': 1002000}
    runtime.collect([metadata], lambda _: buffer.getvalue())
    assert not runtime.process_next()
    item = store.get_inventory_snapshot()['items'][0]
    sessions.update(identity, 'committed', {'receipts': [{'added_item_ids': [item['id']]}]})
    assert runtime.process_next()
    assert store.get_inventory_snapshot()['items'][0]['expires_on'] == '2027-12-01'
    runtime.collect([metadata], lambda _: pytest.fail('duplicate download'))
    assert not runtime.process_next()
    assert json.loads((root / 'audio.json').read_text())['audio_offset_ms'] == 2000
    assert (root / 'transcript.json').is_file() and (root / 'voice-result.json').is_file()


def test_unified_http_service_search_reminders_and_receipts(tmp_path):
    app = create_app(tmp_path / 'api.sqlite3', workers=False)
    store = app.state.store
    store.create_item('cabinet-01', {'name': '真实坚果'}, 0, 'real-nuts', 'test')
    with TestClient(app) as client:
        assert client.get('/api/inventory', params={'query': '坚果'}).json()['items'][0]['name'] == '真实坚果'
        assert client.get('/api/inventory', params={'query': '不存在'}).json()['items'] == []
        due = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        result = client.post('/api/reminders', json={'message': '吃坚果', 'due_at': due,
            'repeat': 'once', 'idempotency_key': 'web-reminder'})
        assert result.status_code == 200
        app.state.assistant.tasks.tick(due)
        state = client.get('/api/reminders').json()
        assert len(state['deliveries']) == 2
        delivery = next(d for d in state['deliveries'] if d['channel'] == 'voice')
        assert client.post('/api/reminders/deliveries/' + delivery['id'] + '/ack',
            json={'channel': 'voice', 'kind': 'played'}).json()['state'] == 'played'
        assert client.get('/api/voice/health').status_code == 200


@pytest.mark.parametrize('transcript', ['你好小盒，坚果在哪里？', '你好小盒坚果在哪里？', '你好，小盒，坚果在哪里？',
                                       '你好小河，坚果在哪里？'])
def test_wake_speech_queries_real_inventory_without_a_video_session(store, tmp_path, transcript):
    service = assistant(store, [('search_inventory', {'query': '坚果'})])
    service.output = tmp_path / 'voice'
    class Transcriber:
        def transcribe(self, payload):
            assert payload == b'test audio'
            return {'text': transcript, 'segments': []}
    service.transcriber = Transcriber()
    sessions = Sessions(store, tmp_path / 'sessions')
    runtime = VoiceRuntime(service, sessions, 'http://unused.invalid')
    metadata = {'complete': True, 'origin': 'wake', 'session_id': 'audio-wake',
        'cabinet_id': 'cabinet-01', 'wake_word': '你好小盒', 'stream_started_unix_ms': 1002000}
    runtime.collect([metadata], lambda _: b'test audio')
    runtime.collect([metadata], lambda _: pytest.fail('duplicate download'))
    with store.db.connect() as db:
        row = db.execute('SELECT * FROM voice_interactions').fetchone()
        assert (row['origin'], row['state'], row['session_id']) == ('wake', 'queued', None)
    assert runtime.process_next()
    assert not runtime.process_next()
    with store.db.connect() as db:
        row = db.execute('SELECT * FROM voice_interactions').fetchone()
        assert row['state'] == 'completed'
        assert row['transcript'] == '坚果在哪里？'
        result = json.loads(row['result_json'])
        assert result['inventory']['items'][0]['name'] == '坚果'
        assert result['inventory']['items'][0]['drawer'] == '第一层'
        assert not list(db.execute('PRAGMA foreign_key_check'))
    assert sessions.list() == []


def test_keyword_only_does_not_call_remote_ai(store, tmp_path):
    service = assistant(store, [])
    service.output = tmp_path / 'voice'
    service.client.chat = lambda *args, **kwargs: pytest.fail('No question must not call remote AI')
    class Transcriber:
        def transcribe(self, payload):
            return {'text': '你好小盒。', 'segments': []}
    service.transcriber = Transcriber()
    runtime = VoiceRuntime(service, Sessions(store, tmp_path / 'sessions'), 'http://unused.invalid')
    runtime.collect([{'complete': True, 'origin': 'wake', 'session_id': 'audio-keyword',
        'cabinet_id': 'cabinet-01', 'wake_word': '你好小盒', 'stream_started_unix_ms': 1002000}], lambda _: b'audio')
    assert runtime.process_next()
    with store.db.connect() as db:
        row = db.execute('SELECT state,result_json FROM voice_interactions').fetchone()
        assert row['state'] == 'completed'
        assert json.loads(row['result_json'])['status'] == 'no_speech'
