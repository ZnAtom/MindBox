"""Local owner pairing and revocable, cabinet-scoped machine credentials."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets

from .database import Database, DatabaseError, NotFound, encode, new_id, utcnow


SCOPES = frozenset(('inventory:read', 'history:read', 'images:read'))
MIGRATION = '006_agent_access'


class AccessError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def future(**kwargs):
    return (datetime.now(timezone.utc) + timedelta(**kwargs)).isoformat(timespec='microseconds')


@dataclass(frozen=True)
class Principal:
    id: str
    scopes: frozenset
    cabinets: tuple

    def require(self, scope, cabinet=None):
        if cabinet is not None and cabinet not in self.cabinets:
            raise AccessError(404, 'not_found', 'Resource not found')
        if scope not in self.scopes:
            raise AccessError(403, 'insufficient_scope', 'Required permission: ' + scope)


class KeyStore:
    def __init__(self, database):
        self.db = database if isinstance(database, Database) else Database(database)

    def verify_schema(self):
        with self.db.connect() as db:
            if not db.execute('SELECT 1 FROM schema_migrations WHERE version=?', (MIGRATION,)).fetchone():
                raise DatabaseError('Run the local database migration before starting the Agent API')

    def audit(self, request_id, route, status, *, key_id=None, cabinet=None, duration_ms=0):
        with self.db.transaction() as db:
            db.execute('DELETE FROM api_access_logs WHERE created_at < ?', (future(days=-30),))
            db.execute('INSERT INTO api_access_logs VALUES (?,?,?,?,?,?,?)',
                       (request_id, key_id, route, cabinet, status, duration_ms, utcnow()))
            # Cap failed-auth traffic as well as retention time.
            db.execute('DELETE FROM api_access_logs WHERE id IN (SELECT id FROM api_access_logs '
                       'ORDER BY created_at DESC LIMIT -1 OFFSET 20000)')

    def create(self, name, cabinets, scopes, days=90):
        name = name.strip()
        scopes, cabinets = sorted(set(scopes)), sorted(set(cabinets))
        if not 1 <= len(name) <= 80 or not cabinets or not 1 <= days <= 365:
            raise DatabaseError('Invalid connection name, cabinet selection or expiry')
        if 'inventory:read' not in scopes or set(scopes) - SCOPES:
            raise DatabaseError('Invalid connection permissions')
        identity, secret = secrets.token_hex(8), secrets.token_urlsafe(32)
        with self.db.transaction() as db:
            for cabinet in cabinets:
                if not db.execute('SELECT 1 FROM cabinets WHERE id=?', (cabinet,)).fetchone():
                    raise NotFound('Cabinet not found')
            if db.execute('SELECT count(*) FROM api_keys WHERE revoked_at IS NULL AND expires_at>?',
                          (utcnow(),)).fetchone()[0] >= 100:
                raise DatabaseError('At most 100 active connections are supported')
            db.execute('INSERT INTO api_keys VALUES (?,?,?,?,?,?,NULL,NULL)',
                       (identity, name, digest(secret), encode(scopes), utcnow(), future(days=days)))
            db.executemany('INSERT INTO api_key_cabinets VALUES (?,?)', [(identity, c) for c in cabinets])
        self.audit(new_id(), 'owner.keys.create', 201, key_id=identity)
        return {'connection': next(k for k in self.list() if k['id'] == identity),
                'key': f'mbx_{identity}_{secret}'}

    def list(self):
        with self.db.connect() as db:
            result = []
            for row in db.execute('SELECT * FROM api_keys ORDER BY created_at DESC'):
                value = {k: row[k] for k in ('id', 'name', 'created_at', 'expires_at', 'revoked_at', 'last_used_at')}
                value['prefix'] = 'mbx_' + row['id'] + '_'
                value['scopes'] = json.loads(row['scopes_json'])
                value['cabinet_ids'] = [r[0] for r in db.execute(
                    'SELECT cabinet_id FROM api_key_cabinets WHERE key_id=? ORDER BY cabinet_id', (row['id'],))]
                value['status'] = 'revoked' if row['revoked_at'] else 'expired' if row['expires_at'] <= utcnow() else 'active'
                result.append(value)
            return result

    def revoke(self, identity):
        with self.db.transaction() as db:
            if not db.execute('SELECT 1 FROM api_keys WHERE id=?', (identity,)).fetchone():
                raise NotFound('Connection not found')
            db.execute('UPDATE api_keys SET revoked_at=coalesce(revoked_at,?) WHERE id=?', (utcnow(), identity))
        self.audit(new_id(), 'owner.keys.revoke', 200, key_id=identity)

    def rotate(self, identity):
        prior = next((k for k in self.list() if k['id'] == identity and k['status'] == 'active'), None)
        if not prior:
            raise NotFound('Active connection not found')
        return self.create(prior['name'], prior['cabinet_ids'], prior['scopes'])

    def authenticate(self, token):
        parts = token.split('_', 2)
        if len(parts) != 3 or parts[0] != 'mbx' or len(token) > 200:
            raise AccessError(401, 'invalid_key', 'A valid Bearer API key is required')
        with self.db.connect() as db:
            row = db.execute('SELECT * FROM api_keys WHERE id=?', (parts[1],)).fetchone()
            expected = row['secret_hash'] if row else '0' * 64
            matches = hmac.compare_digest(expected, digest(parts[2]))
            if not matches or not row or row['revoked_at'] or row['expires_at'] <= utcnow():
                raise AccessError(401, 'invalid_key', 'A valid Bearer API key is required')
            cabinets = tuple(r[0] for r in db.execute('SELECT cabinet_id FROM api_key_cabinets WHERE key_id=?', (row['id'],)))
            return Principal(row['id'], frozenset(json.loads(row['scopes_json'])), cabinets)

    def used(self, identity):
        with self.db.transaction() as db:
            db.execute('UPDATE api_keys SET last_used_at=? WHERE id=?', (utcnow(), identity))

    def pairing_code(self):
        token = secrets.token_urlsafe(32)
        with self.db.transaction() as db:
            db.execute("DELETE FROM api_owner_tokens WHERE kind='pairing' OR expires_at<=?", (utcnow(),))
            db.execute('INSERT INTO api_owner_tokens VALUES (?,?,?,?,?,NULL)',
                       (new_id(), digest(token), 'pairing', utcnow(), future(minutes=10)))
        return token

    def pair(self, token):
        session = secrets.token_urlsafe(32)
        with self.db.transaction() as db:
            row = db.execute("SELECT * FROM api_owner_tokens WHERE token_hash=? AND kind='pairing'",
                             (digest(token),)).fetchone()
            if not row or row['consumed_at'] or row['expires_at'] <= utcnow():
                raise AccessError(401, 'invalid_pairing', '配对码无效或已过期')
            db.execute('UPDATE api_owner_tokens SET consumed_at=? WHERE id=?', (utcnow(), row['id']))
            db.execute('INSERT INTO api_owner_tokens VALUES (?,?,?,?,?,NULL)',
                       (new_id(), digest(session), 'session', utcnow(), future(hours=8)))
        return session

    def owner(self, token):
        with self.db.connect() as db:
            row = db.execute("SELECT 1 FROM api_owner_tokens WHERE token_hash=? AND kind='session' AND expires_at>?",
                             (digest(token or ''), utcnow())).fetchone()
        if not row:
            raise AccessError(401, 'owner_required', '请先完成本机配对')

    def logout(self, token):
        with self.db.transaction() as db:
            db.execute("DELETE FROM api_owner_tokens WHERE token_hash=? AND kind='session'", (digest(token or ''),))
