"""Live voice actions reuse speech provenance and the durable reminder scheduler."""
from datetime import datetime, timedelta
import hashlib
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .database import DatabaseError, encode, fingerprint, timestamp, utcnow
from .scheduling import Scheduling
from .speech import SpeechService, _subject_match, normalized_facts, validate_slots


def tool(name, description, properties, required):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': required,
                       'additionalProperties': False}}}


VOICE_TOOLS = [
    tool('record_item_facts', '记录用户明确陈述的物品生产日期、到期日期、保质期。所有 raw 字段必须逐字引用原文；缺少的字段传 null。不能从常识推测日期。', {
        'subject_raw': {'type': 'string'},
        **{key: {'type': ['string', 'null']} for key in ('production_raw', 'expiry_raw', 'shelf_life_raw')},
    }, ['subject_raw', 'production_raw', 'expiry_raw', 'shelf_life_raw']),
    tool('schedule_reminder', '创建一次或每天的定时提醒。due_at 为带时区的 ISO 时间；结合当前时间解释明天、十分钟后等。缺少时间请追问。', {
        'message': {'type': 'string'}, 'due_at': {'type': 'string'},
        'repeat': {'type': 'string', 'enum': ['once', 'daily']},
        'evidence': {'type': 'string', 'description': '逐字引用用户要求提醒的原话'},
        'item_query': {'type': ['string', 'null'], 'description': '关联物品名称；无关联时为 null'},
    }, ['message', 'due_at', 'repeat', 'evidence', 'item_query']),
    tool('list_reminders', '查询已保存的提醒及其 ID、时间、状态。', {}, []),
    tool('cancel_reminder', '取消指定提醒或每天提醒。先查询真实提醒 ID。', {
        'reminder_id': {'type': 'string'}, 'evidence': {'type': 'string'},
    }, ['reminder_id', 'evidence']),
]


class LiveTasks:
    def __init__(self, store, timezone='Asia/Shanghai'):
        self.store, self.timezone = store, timezone
        self.scheduler = Scheduling(store.db)

    def resolve_item(self, subject, cabinet, scope=None):
        items = self.store.get_inventory_snapshot(cabinet)['items']
        layer = next((index for index, label in enumerate(('第一层', '第二层', '第三层'), 1) if label in subject), None)
        if layer:
            items = [i for i in items if i['location_id'] == f'{cabinet}:drawer-{layer}']
        if scope:
            items = [i for i in items if i['id'] in scope]
        matches = [i for i in items if subject and (subject in i['name'] or i['name'] in subject)]
        if not matches and subject in ('这个', '这个物品', '这件', '这盒', '它'):
            matches = [i for i in items if i['id'] in (scope or [])]
        if not matches and scope:
            scored = [(i, _subject_match(subject, i)[0]) for i in items if i['id'] in scope]
            best = max((score for _, score in scored), default=0)
            matches = [i for i, score in scored if score == best and score > 0]
        if len(matches) != 1:
            return None, {'status': 'needs_input', 'reason': '请说明是哪件物品，同名多件时需明确区分。',
                          'candidates': [{'id': i['id'], 'name': i['name'], 'location_id': i['location_id']}
                                         for i in matches]}
        return matches[0], None

    def record_facts(self, arguments, text, cabinet, interaction, *, scope=None, clock=None, audio=None):
        slots = validate_slots(text, {**arguments, 'commands': [], 'uncertainties': []})
        item, problem = self.resolve_item(slots['subject_raw'], cabinet, scope)
        if problem:
            return problem
        clock = clock or {'quality': 'trusted', 'capture_start': datetime.now(ZoneInfo(self.timezone)).isoformat()}
        facts = normalized_facts(slots, clock)
        identity = 'live-' + fingerprint([interaction, slots])[:32]
        parsed = {'slots': slots, 'facts': facts, 'link': {'policy': 'unique-live-inventory-match'},
                  'origin': 'live', 'interaction_id': interaction}
        audio_path = str(audio) if audio else ''
        digest = hashlib.sha256(audio.read_bytes()).hexdigest() if audio else ''
        with self.store.db.transaction() as db:
            db.execute('INSERT OR IGNORE INTO speech_utterances '
                '(id,cabinet_id,source_video_hash,audio_path,audio_sha256,start_ms,end_ms,text,version,extractor,'
                'clock_json,parsed_json,item_id,link_status,body_hash,created_at) VALUES (?,?,?,?,?,0,0,?,1,?,?,?,?,?,?,?)',
                (identity, cabinet, interaction, audio_path, digest, text, 'live-voice-v1', encode(clock),
                 encode(parsed), item['id'], 'linked', fingerprint([text, slots]), utcnow()))
        service = SpeechService(SimpleNamespace(store=self.store), cabinet)
        result = service.apply_facts(identity)
        return {'status': 'applied' if 'revision' in result else result.get('status', 'pending'),
                'item_id': item['id'], 'name': item['name'], 'facts': facts, 'receipt': result}

    def schedule(self, cabinet, message, due_at, key, repeat='once', item_id=None):
        if repeat not in ('once', 'daily'):
            raise DatabaseError('不支持的重复规则')
        if not message.strip() or len(message) > 500:
            raise DatabaseError('提醒内容不能为空或超过 500 字')
        due_at = timestamp(due_at)
        # The reminder and its repeat rule must become visible together.
        with self.store.db.transaction() as db:
            reminder = self.scheduler._create_reminder(db, cabinet, message, due_at, key,
                                                       ('web', 'voice'), item_id)
            if repeat == 'daily':
                db.execute('INSERT OR IGNORE INTO live_reminder_series VALUES (?,?,?,1)',
                           (reminder['id'], reminder['id'], self.timezone))
        return {'status': 'scheduled', 'id': reminder['id'], 'message': message,
                'due_at': due_at, 'repeat': repeat}

    def reminders(self, cabinet):
        values = self.scheduler.list_reminders(cabinet)
        with self.store.db.connect() as db:
            series = {row['current_id']: dict(row) for row in db.execute('SELECT * FROM live_reminder_series')}
        for value in values:
            value['repeat'] = 'daily' if value['id'] in series and series[value['id']]['enabled'] else 'once'
            value['channels'] = json.loads(value.pop('channels_json'))
        return values

    def cancel(self, identity, cabinet):
        with self.store.db.transaction() as db:
            row = db.execute('SELECT cabinet_id FROM reminders WHERE id=?', (identity,)).fetchone()
            if not row or row[0] != cabinet:
                raise DatabaseError('提醒不存在')
            series = db.execute('SELECT * FROM live_reminder_series WHERE id=? OR current_id=?',
                                (identity, identity)).fetchone()
            if series:
                db.execute('UPDATE live_reminder_series SET enabled=0 WHERE id=?', (series['id'],))
                self.scheduler._cancel_reminder(db, series['current_id'])
            return self.scheduler._cancel_reminder(db, identity)

    def tick(self, at=None):
        at = timestamp(at) if at else utcnow()
        self.scheduler.trigger_due(at)
        with self.store.db.transaction() as db:
            rows = db.execute('SELECT s.*,r.* FROM live_reminder_series s '
                'JOIN reminders r ON r.id=s.current_id WHERE s.enabled=1 '
                "AND r.status IN ('triggered','missed')").fetchall()
            for row in rows:
                zone = ZoneInfo(row['timezone'])
                due = datetime.fromisoformat(row['due_at']).astimezone(zone)
                local_now = datetime.fromisoformat(at).astimezone(zone)
                due += timedelta(days=max(1, (local_now.date() - due.date()).days))
                if due <= local_now:
                    due += timedelta(days=1)
                reminder = self.scheduler._create_reminder(db, row['cabinet_id'], row['message'], due.isoformat(),
                    'repeat:' + row['id'] + ':' + due.date().isoformat(), ('web', 'voice'), row['item_id'], at)
                db.execute('UPDATE live_reminder_series SET current_id=? WHERE current_id=?',
                           (reminder['id'], row['current_id']))

    def run(self, stopped):
        while not stopped.is_set():
            self.tick()
            stopped.wait(.5)

    def deliveries(self, cabinet):
        with self.store.db.connect() as db:
            return [dict(row) for row in db.execute('SELECT d.*,r.message,r.due_at FROM deliveries d '
                'JOIN reminders r ON r.id=d.reminder_id WHERE r.cabinet_id=? '
                "AND (d.state='queued' OR (d.channel='voice' AND d.state IN ('received','failed'))) ORDER BY r.due_at", (cabinet,))]

    def acknowledge(self, identity, channel, kind, cabinet):
        with self.store.db.connect() as db:
            row = db.execute('SELECT r.cabinet_id FROM deliveries d JOIN reminders r ON r.id=d.reminder_id '
                             'WHERE d.id=?', (identity,)).fetchone()
        if not row or row[0] != cabinet:
            raise DatabaseError('提醒回执不存在')
        result = self.scheduler.acknowledge(identity, channel, kind)
        if kind in ('received', 'played', 'failed'):
            with self.store.db.transaction() as db:
                db.execute("UPDATE jobs SET state='done' WHERE business_key=? AND state='queued'",
                           ('delivery:' + identity,))
        return result
