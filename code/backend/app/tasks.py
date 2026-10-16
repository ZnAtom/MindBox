"""Minimal speech-task drafts and event rules on the existing durable scheduler."""
from datetime import datetime, timedelta
import json

from .database import Conflict, DatabaseError, encode, fingerprint, new_id, timestamp, utcnow
from .scheduling import Scheduling
from .speech import SpeechService
from .store import item_value, require


class Tasks:
    def __init__(self, context, cabinet_id='cabinet-01'):
        self.context, self.db, self.cabinet_id = context, context.store.db, cabinet_id
        self.scheduling = Scheduling(self.db)

    def draft(self, utterance_id, removal_policy_reason=None):
        utterance = SpeechService(self.context, self.cabinet_id).get(utterance_id)
        slots = [{**slot, 'origin': 'speech'} for slot in utterance['parsed']['slots']['commands']]
        if removal_policy_reason:
            if not isinstance(removal_policy_reason, str) or not removal_policy_reason.strip():
                raise DatabaseError('Configured policy needs an explicit reason')
            if not any(s['kind'] == 'after_expiry_remove' for s in slots):
                slots.append({'kind': 'after_expiry_remove', 'raw': removal_policy_reason, 'origin': 'configured_policy'})
        result = []
        with self.db.transaction() as connection:
            for slot in slots:
                existing = connection.execute('SELECT * FROM task_commands WHERE utterance_id=? AND kind=?',
                                              (utterance_id, slot['kind'])).fetchone()
                if existing:
                    result.append(dict(existing))
                    continue
                identity = new_id()
                data = {'raw': slot['raw'], 'origin': slot['origin'], 'speech_version': utterance['version'],
                        'missing': ['item_link'] if not utterance['item_id'] else [],
                        'request': 'eat_before_expiry' if slot['kind'] == 'before_expiry' else 'warn_on_expired_removal'}
                data['missing'] += ['expiry_date', 'expiry_boundary']
                if slot['kind'] == 'before_expiry':
                    data['missing'].append('reminder_at')
                connection.execute('INSERT INTO task_commands VALUES (?, ?, ?, ?, ?, ?, 1, ?, NULL, ?)',
                    (identity, self.cabinet_id, utterance_id, utterance['item_id'], slot['kind'],
                     'needs_input', encode(data), fingerprint([utterance_id, slot])))
                result.append(require(connection, 'task_commands', identity))
        return result

    def complete(self, command_id, expected_version, *, expiry_cutoff, due_at=None, at=None):
        """Explicit user-supplied instants; do not infer a default hour or date boundary."""
        at = timestamp(at) if at else utcnow()
        cutoff = timestamp(expiry_cutoff)
        due = timestamp(due_at) if due_at else None
        request_hash = fingerprint([command_id, expected_version, cutoff, due])
        with self.db.transaction() as connection:
            command = require(connection, 'task_commands', command_id)
            data = json.loads(command['data_json'])
            if data.get('completion_hash') == request_hash:
                return {**command, 'data': data}
            if command['cabinet_id'] != self.cabinet_id or command['version'] != expected_version or command['status'] != 'needs_input':
                raise Conflict('Task draft changed or is already active')
            utterance = require(connection, 'speech_utterances', command['utterance_id'])
            if utterance['link_status'] != 'linked' or utterance['version'] != data['speech_version']:
                raise Conflict('Speech link/version must be revalidated before task activation')
            item = item_value(require(connection, 'items', utterance['item_id']))
            if not item.get('expires_on'):
                raise DatabaseError('A recorded expiry date is required; missing years cannot schedule a task')
            local_cutoff = datetime.fromisoformat(expiry_cutoff.replace('Z', '+00:00'))
            expiry = datetime.fromisoformat(item['expires_on']).date()
            if local_cutoff.date() not in (expiry, expiry + timedelta(days=1)):
                raise DatabaseError('Explicit expiry cutoff must agree with the recorded local expiry date')
            if local_cutoff.date() > expiry and any((local_cutoff.hour, local_cutoff.minute, local_cutoff.second, local_cutoff.microsecond)):
                raise DatabaseError('Following-day expiry boundary must be midnight')
            reminder_id = None
            if command['kind'] == 'before_expiry':
                if not due or not at < due < cutoff:
                    raise DatabaseError('Explicit future reminder time before expiry cutoff is required')
                reminder = self.scheduling._create_reminder(connection, self.cabinet_id,
                    '请在到期前食用：' + item['name'], due, 'task:' + command_id + ':v' + str(expected_version + 1),
                    ('web',), item['id'], at)
                reminder_id, status = reminder['id'], 'scheduled'
            else:
                if due:
                    raise DatabaseError('An event rule does not take a countdown time')
                status = 'active'
                condition = {'kind': 'confirmed_remove_after_expiry', 'expires_on': item['expires_on'],
                             'expiry_cutoff': cutoff, 'speech_version': utterance['version']}
                connection.execute('INSERT INTO item_event_rules VALUES (?, ?, ?, ?, ?, ?, 1) '
                    'ON CONFLICT(command_id) DO UPDATE SET item_id=excluded.item_id, version=excluded.version, '
                    'condition_json=excluded.condition_json, created_at=excluded.created_at, enabled=1',
                    (new_id(), command_id, item['id'], expected_version + 1, encode(condition), at))
            data.update(missing=[], expiry_cutoff=cutoff, due_at=due, expires_on=item['expires_on'],
                        completion_hash=request_hash, supplied_at=at)
            connection.execute('UPDATE task_commands SET item_id=?, status=?, version=version+1, data_json=?, reminder_id=? WHERE id=?',
                (item['id'], status, encode(data), reminder_id, command_id))
            return {**require(connection, 'task_commands', command_id), 'data': data}

    def invalidate_changed_dates(self):
        invalidated = []
        with self.db.transaction() as connection:
            for command in connection.execute("SELECT * FROM task_commands WHERE cabinet_id=? AND status IN ('scheduled','active')",
                                              (self.cabinet_id,)).fetchall():
                data = json.loads(command['data_json'])
                item = item_value(require(connection, 'items', command['item_id']))
                if item['expires_on'] == data.get('expires_on'):
                    continue
                if command['reminder_id']:
                    self.scheduling._cancel_reminder(connection, command['reminder_id'])
                connection.execute('UPDATE item_event_rules SET enabled=0 WHERE command_id=?', (command['id'],))
                data.update(missing=['expiry_changed_revalidate'], completion_hash=None)
                connection.execute("UPDATE task_commands SET status='needs_input', version=version+1, data_json=? WHERE id=?",
                                   (encode(data), command['id']))
                invalidated.append(command['id'])
        return invalidated

    def cancel(self, command_id, expected_version):
        with self.db.transaction() as connection:
            command = require(connection, 'task_commands', command_id)
            if command['cabinet_id'] != self.cabinet_id:
                raise DatabaseError('Wrong task cabinet')
            if command['status'] == 'canceled' and command['version'] == expected_version + 1:
                return command
            if command['version'] != expected_version:
                raise Conflict('Task version changed')
            if command['reminder_id']:
                self.scheduling._cancel_reminder(connection, command['reminder_id'])
            connection.execute('UPDATE item_event_rules SET enabled=0 WHERE command_id=?', (command_id,))
            connection.execute("UPDATE task_commands SET status='canceled', version=version+1 WHERE id=?", (command_id,))
            return require(connection, 'task_commands', command_id)

    def evaluate_remove(self, event_id, at=None):
        at = timestamp(at) if at else utcnow()
        self.invalidate_changed_dates()
        clock = self.context.manifest.get('capture_clock') or {}
        if clock.get('quality') not in ('trusted', 'synthetic_test') or not clock.get('capture_start'):
            return {'status': 'pending', 'reason': 'no_trusted_event_clock', 'reminders': []}
        with self.db.transaction() as connection:
            event = require(connection, 'verified_events', event_id)
            item = item_value(require(connection, 'items', event['item_id']))
            payload = json.loads(event['payload_json'])
            if item['cabinet_id'] != self.cabinet_id or payload.get('kind') != 'remove' or not payload.get('committed'):
                raise DatabaseError('Only an in-scope committed removal event can trigger this rule')
            source = connection.execute('SELECT m.input_sha256 FROM inventory_commits c JOIN operations p ON p.id=c.operation_id '
                'JOIN media_captures m ON m.id=p.capture_id WHERE c.id=?', (event['commit_id'],)).fetchone()
            if not source or source[0] not in {v['sha256'] for v in self.context.manifest['videos']}:
                raise DatabaseError('Removal capture is not covered by this experiment clock')
            event_at = timestamp((datetime.fromisoformat(timestamp(clock['capture_start'])) +
                                  timedelta(milliseconds=payload['event_window_ms'][1])).isoformat())
            if event_at > at:
                return {'status': 'pending', 'reason': 'event_is_in_future', 'reminders': []}
            reminders = []
            for rule in connection.execute('SELECT r.* FROM item_event_rules r JOIN task_commands c ON c.id=r.command_id '
                "WHERE r.item_id=? AND r.enabled=1 AND c.status='active' AND c.version=r.version", (item['id'],)).fetchall():
                condition = json.loads(rule['condition_json'])
                if event_at < rule['created_at'] or event_at < condition['expiry_cutoff']:
                    continue
                prior = connection.execute('SELECT reminder_id FROM rule_firings WHERE rule_id=? AND rule_version=? AND event_id=?',
                                           (rule['id'], rule['version'], event_id)).fetchone()
                if prior:
                    reminders.append(prior[0])
                    continue
                due = (datetime.fromisoformat(at) + timedelta(seconds=1)).isoformat()
                reminder = self.scheduling._create_reminder(connection, self.cabinet_id,
                    '该物品已过期，取出时请注意：' + item['name'], due,
                    f"rule:{rule['id']}:{rule['version']}:{event_id}", ('web',), item['id'], at)
                connection.execute('INSERT INTO rule_firings VALUES (?, ?, ?, ?, ?)',
                    (new_id(), rule['id'], rule['version'], event_id, reminder['id']))
                reminders.append(reminder['id'])
        return {'status': 'evaluated', 'event_at': event_at, 'clock_quality': clock['quality'],
                'reminders': reminders, 'task_completed': False}

    def list(self):
        with self.db.connect() as connection:
            return [{**dict(r), 'data': json.loads(r['data_json'])} for r in connection.execute(
                'SELECT * FROM task_commands WHERE cabinet_id=? ORDER BY id', (self.cabinet_id,))]

    def scan_rules(self, at=None):
        with self.db.connect() as connection:
            events = [r[0] for r in connection.execute('SELECT DISTINCT e.id FROM verified_events e '
                'JOIN item_event_rules r ON r.item_id=e.item_id JOIN task_commands c ON c.id=r.command_id '
                "WHERE c.cabinet_id=? AND c.status='active' AND r.enabled=1 AND json_extract(e.payload_json,'$.kind')='remove'",
                (self.cabinet_id,))]
        return [{'event_id': identity, **self.evaluate_remove(identity, at)} for identity in events]
