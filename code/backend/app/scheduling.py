"""Durable job leases and reminder storage; network/audio delivery is separate."""
from datetime import datetime, timedelta
import json

from .database import Conflict, DatabaseError, encode, fingerprint, new_id, timestamp, utcnow
from .store import require


def later(at, seconds):
    return (datetime.fromisoformat(at) + timedelta(seconds=seconds)).isoformat(timespec='microseconds')


class Scheduling:
    def __init__(self, database):
        self.db = database

    def enqueue(self, business_key, kind, payload, at=None, max_attempts=3):
        at = timestamp(at) if at else utcnow()
        if not business_key or not kind or type(max_attempts) is not int or max_attempts < 1:
            raise DatabaseError('Invalid job configuration')
        with self.db.transaction() as connection:
            existing = connection.execute('SELECT * FROM jobs WHERE business_key = ?', (business_key,)).fetchone()
            if existing:
                if (existing['kind'], existing['payload_json'], existing['max_attempts']) != (kind, encode(payload), max_attempts):
                    raise Conflict('Job key already has different content')
                return dict(existing)
            identity = new_id()
            connection.execute('INSERT INTO jobs (id, business_key, kind, payload_json, max_attempts, next_run_at) '
                               'VALUES (?, ?, ?, ?, ?, ?)', (identity, business_key, kind, encode(payload), max_attempts, at))
            return require(connection, 'jobs', identity)

    def claim_job(self, at=None, lease_seconds=60, kinds=None):
        at = timestamp(at) if at else utcnow()
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise DatabaseError('Positive integer lease duration required')
        with self.db.transaction() as connection:
            connection.execute("UPDATE jobs SET state = 'failed', lease_token = NULL, lease_expires_at = NULL "
                "WHERE state = 'running' AND lease_expires_at <= ? AND attempts >= max_attempts", (at,))
            sql = "SELECT * FROM jobs WHERE attempts < max_attempts AND ((state = 'queued' AND next_run_at <= ?) "
            sql += "OR (state = 'running' AND lease_expires_at <= ?))"
            params = [at, at]
            if kinds is not None:
                if not kinds:
                    return None
                sql += ' AND kind IN (' + ','.join('?' for _ in kinds) + ')'
                params.extend(kinds)
            row = connection.execute(sql + ' ORDER BY next_run_at, id LIMIT 1', params).fetchone()
            if not row:
                return None
            connection.execute("UPDATE jobs SET state = 'running', attempts = attempts + 1, lease_token = ?, "
                'lease_expires_at = ? WHERE id = ?', (new_id(), later(at, lease_seconds), row['id']))
            return require(connection, 'jobs', row['id'])

    def finish_job(self, job_id, lease_token, error=None, at=None, retry_seconds=5):
        at = timestamp(at) if at else utcnow()
        if type(retry_seconds) is not int or retry_seconds < 0:
            raise DatabaseError('Retry duration must be nonnegative')
        with self.db.transaction() as connection:
            job = require(connection, 'jobs', job_id)
            if job['state'] != 'running' or job['lease_token'] != lease_token or job['lease_expires_at'] <= at:
                raise Conflict('Job lease expired or belongs to another worker')
            state = 'done' if error is None else 'failed' if job['attempts'] >= job['max_attempts'] else 'queued'
            connection.execute('UPDATE jobs SET state = ?, last_error = ?, next_run_at = ?, '
                'lease_token = NULL, lease_expires_at = NULL WHERE id = ?',
                (state, error, later(at, retry_seconds), job_id))
            return require(connection, 'jobs', job_id)

    def renew_lease(self, job_id, lease_token, at=None, lease_seconds=60):
        at = timestamp(at) if at else utcnow()
        if type(lease_seconds) is not int or lease_seconds < 1:
            raise DatabaseError('Positive lease duration required')
        with self.db.transaction() as connection:
            changed = connection.execute("UPDATE jobs SET lease_expires_at = ? WHERE id = ? AND state = 'running' "
                'AND lease_token = ? AND lease_expires_at > ?',
                (later(at, lease_seconds), job_id, lease_token, at)).rowcount
            if changed != 1:
                raise Conflict('Cannot renew an expired or superseded lease')

    def create_reminder(self, cabinet_id, message, due_at, idempotency_key, channels=('web',), item_id=None, at=None):
        due_at, at = timestamp(due_at), timestamp(at) if at else utcnow()
        channels = sorted(set(channels))
        if not message.strip() or not idempotency_key.strip() or not channels or set(channels) - {'web', 'voice'}:
            raise DatabaseError('Reminder requires message, key and supported channels')
        body_hash = fingerprint([message, due_at, channels, item_id])
        with self.db.transaction() as connection:
            require(connection, 'cabinets', cabinet_id)
            existing = connection.execute('SELECT * FROM reminders WHERE cabinet_id = ? AND idempotency_key = ?',
                                          (cabinet_id, idempotency_key)).fetchone()
            if existing:
                if existing['body_hash'] != body_hash:
                    raise Conflict('Reminder key already has different content')
                return dict(existing)
            if due_at <= at:
                raise DatabaseError('New reminder must have a future due_at')
            if item_id and require(connection, 'items', item_id)['cabinet_id'] != cabinet_id:
                raise DatabaseError('Reminder item belongs to another cabinet')
            identity = new_id()
            connection.execute('INSERT INTO reminders VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)',
                (identity, cabinet_id, item_id, message, due_at, encode(channels), 'pending', idempotency_key, body_hash, at))
            return require(connection, 'reminders', identity)

    def trigger_due(self, at=None, grace_seconds=600):
        at = timestamp(at) if at else utcnow()
        if type(grace_seconds) is not int or grace_seconds < 0:
            raise DatabaseError('Invalid reminder grace period')
        triggered, missed = [], []
        with self.db.transaction() as connection:
            for reminder in connection.execute("SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ?", (at,)).fetchall():
                expired = later(reminder['due_at'], grace_seconds) < at
                connection.execute('UPDATE reminders SET status = ?, triggered_at = ? WHERE id = ?',
                                   ('missed' if expired else 'triggered', at, reminder['id']))
                if expired:
                    missed.append(reminder['id'])
                    continue
                for channel in json.loads(reminder['channels_json']):
                    identity = new_id()
                    connection.execute('INSERT INTO deliveries VALUES (?, ?, ?, ?, NULL, NULL)',
                                       (identity, reminder['id'], channel, 'queued'))
                    connection.execute('INSERT INTO jobs (id, business_key, kind, payload_json, next_run_at) VALUES (?, ?, ?, ?, ?)',
                        (new_id(), 'delivery:' + identity, 'reminder.deliver', encode({'delivery_id': identity,
                         'channel': channel, 'message': reminder['message']}), at))
                triggered.append(reminder['id'])
        return {'triggered': triggered, 'missed': missed}

    def cancel_reminder(self, reminder_id):
        with self.db.transaction() as connection:
            reminder = require(connection, 'reminders', reminder_id)
            connection.execute("UPDATE reminders SET status = 'canceled' WHERE id = ? AND status = 'pending'", (reminder_id,))
            deliveries = connection.execute("SELECT id FROM deliveries WHERE reminder_id = ? AND state = 'queued'", (reminder_id,)).fetchall()
            for delivery in deliveries:
                job = connection.execute('SELECT * FROM jobs WHERE business_key = ?', ('delivery:' + delivery['id'],)).fetchone()
                # A worker may already have sent a claimed job. Do not claim that it was revoked.
                if job and job['state'] == 'queued':
                    connection.execute("UPDATE jobs SET state = 'done' WHERE id = ?", (job['id'],))
                    connection.execute("UPDATE deliveries SET state = 'canceled' WHERE id = ?", (delivery['id'],))
            return {'previous_status': reminder['status'], **require(connection, 'reminders', reminder_id)}

    def acknowledge(self, delivery_id, channel, kind, at=None):
        if kind not in ('received', 'played', 'failed'):
            raise DatabaseError('Unsupported receipt')
        at = timestamp(at) if at else utcnow()
        with self.db.transaction() as connection:
            delivery = require(connection, 'deliveries', delivery_id)
            if channel != delivery['channel'] or (kind == 'played' and channel != 'voice'):
                raise DatabaseError('Receipt does not match the delivery channel')
            if delivery['state'] in ('played', 'canceled') or (delivery['state'] == 'received' and kind in ('received', 'failed')):
                return delivery
            connection.execute('UPDATE deliveries SET state = ?, received_at = COALESCE(received_at, ?), '
                'played_at = COALESCE(played_at, ?) WHERE id = ?',
                (kind, at if kind in ('received', 'played') else None, at if kind == 'played' else None, delivery_id))
            return require(connection, 'deliveries', delivery_id)

    def list_reminders(self, cabinet_id):
        with self.db.connect() as connection:
            require(connection, 'cabinets', cabinet_id)
            return [{**dict(r), 'deliveries': [dict(d) for d in connection.execute('SELECT * FROM deliveries WHERE reminder_id = ?', (r['id'],))]}
                    for r in connection.execute('SELECT * FROM reminders WHERE cabinet_id = ? ORDER BY due_at', (cabinet_id,))]
