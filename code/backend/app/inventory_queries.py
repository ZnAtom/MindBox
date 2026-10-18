"""Authorized read projections. No model calls or inventory mutations."""
import base64
import json
from pathlib import Path

from .api_keys import AccessError
from .database import Database, encode, fingerprint, utcnow
from .store import item_value


FIELDS = ('id', 'cabinet_id', 'name', 'name_kind', 'category', 'description', 'brand', 'variant',
          'presence_status', 'identity_status', 'action_status', 'product_match_status', 'stored_at',
          'first_seen_at', 'last_seen_at', 'production_on', 'expires_on', 'quantity', 'unit', 'version', 'updated_at')


def missing():
    raise AccessError(404, 'not_found', 'Resource not found')


def page_cursor(revision, filters, offset):
    return base64.urlsafe_b64encode(encode({'revision': revision, 'filters': fingerprint(filters),
                                          'offset': offset}).encode()).decode()


def page_offset(cursor, revision, filters):
    if not cursor:
        return 0
    try:
        if len(cursor) > 1024:
            raise ValueError()
        value = json.loads(base64.b64decode(cursor, altchars=b'-_', validate=True))
        if value['filters'] != fingerprint(filters):
            raise ValueError()
        offset = value['offset']
        if type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise ValueError()
        if value['revision'] != revision:
            raise AccessError(409, 'snapshot_changed', 'Inventory changed; restart from the first page')
        return offset
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise AccessError(422, 'invalid_cursor', 'Invalid pagination cursor') from None


class InventoryQueries:
    def __init__(self, database):
        self.db = database if isinstance(database, Database) else Database(database)

    def cabinets(self, principal):
        with self.db.connect() as db:
            result = []
            for identity in principal.cabinets:
                row = db.execute('SELECT id,name,inventory_revision,inventory_known FROM cabinets WHERE id=?', (identity,)).fetchone()
                if row:
                    result.append({**dict(row), 'inventory_known': bool(row['inventory_known']),
                        'locations': [dict(r) for r in db.execute('SELECT id,label FROM locations WHERE cabinet_id=? ORDER BY id', (identity,))]})
            return {'cabinets': result}

    def _item(self, db, identity, principal):
        row = db.execute('SELECT * FROM items WHERE id=?', (identity,)).fetchone()
        if not row:
            missing()
        principal.require('inventory:read', row['cabinet_id'])
        return row

    def project(self, db, row, principal):
        value = item_value(row)
        result = {key: value.get(key) for key in FIELDS}
        location = db.execute('SELECT id,label FROM locations WHERE id=? AND cabinet_id=?',
                              (value['location_id'], value['cabinet_id'])).fetchone()
        result['location'] = dict(location) if location else None
        result['last_observed_at'] = value.get('last_seen_at')
        result['field_meta'] = {key: {k: meta.get(k) for k in ('source', 'certainty')}
                                for key, meta in value.get('field_meta', {}).items() if key in FIELDS}
        if 'images:read' in principal.scopes:
            result['images'] = [{'id': r[0], 'url': f'/v1/items/{value["id"]}/images/{r[0]}'} for r in db.execute(
                "SELECT o.id FROM observations o JOIN item_observations io ON io.observation_id=o.id "
                "WHERE io.item_id=? AND o.kind='crop' ORDER BY o.rowid DESC LIMIT 3", (value['id'],))]
        return result

    def inventory(self, principal, cabinet, *, q=None, category=None, location_id=None,
                  presence_status=None, include_removed=False, limit=20, cursor=None):
        principal.require('inventory:read', cabinet)
        filters = dict(cabinet=cabinet, q=q, category=category, location_id=location_id,
                       presence_status=presence_status, include_removed=include_removed, limit=limit)
        with self.db.connect() as db:
            db.execute('BEGIN')
            cabinet_row = db.execute('SELECT * FROM cabinets WHERE id=?', (cabinet,)).fetchone()
            if not cabinet_row:
                missing()
            revision = cabinet_row['inventory_revision']
            offset = page_offset(cursor, revision, filters)
            conditions, params = ['cabinet_id=?'], [cabinet]
            if presence_status is not None:
                conditions.append('presence_status=?')
                params.append(presence_status)
            elif not include_removed:
                conditions.append("presence_status!='removed'")
            for key, value in (('category', category), ('location_id', location_id)):
                if value is not None:
                    conditions.append(key + '=?')
                    params.append(value)
            if q:
                conditions.append('(instr(lower(name),lower(?))>0 OR instr(lower(category),lower(?))>0)')
                params.extend((q, q))
            if location_id and not db.execute('SELECT 1 FROM locations WHERE id=? AND cabinet_id=?', (location_id, cabinet)).fetchone():
                missing()
            rows = db.execute('SELECT * FROM items WHERE ' + ' AND '.join(conditions) +
                              ' ORDER BY created_at,id LIMIT ? OFFSET ?', (*params, limit + 1, offset)).fetchall()
            pending = db.execute('SELECT count(DISTINCT o.id) FROM pending_candidates p JOIN processing_runs r ON r.id=p.run_id '
                'JOIN operations o ON o.id=r.operation_id WHERE o.cabinet_id=? AND p.resolved_at IS NULL '
                'AND r.attempt_no=o.current_attempt', (cabinet,)).fetchone()[0]
            sessions = {r[0]: r[1] for r in db.execute('SELECT state,count(*) FROM automatic_sessions WHERE cabinet_id=? GROUP BY state', (cabinet,))}
            return {'cabinet_id': cabinet, 'inventory_revision': revision, 'generated_at': utcnow(),
                'inventory_known': bool(cabinet_row['inventory_known']),
                'completeness': 'known_baseline' if cabinet_row['inventory_known'] else 'unknown_baseline',
                'pending_operation_count': pending, 'recording_sessions': {k: sessions.get(k, 0)
                    for k in ('capturing', 'queued', 'processing', 'pending', 'failed')},
                'source_live_status': 'unknown', 'items': [self.project(db, r, principal) for r in rows[:limit]],
                'next_cursor': page_cursor(revision, filters, offset + limit) if len(rows) > limit else None}

    def item(self, principal, identity):
        with self.db.connect() as db:
            db.execute('BEGIN')
            row = self._item(db, identity, principal)
            revision = db.execute('SELECT inventory_revision FROM cabinets WHERE id=?', (row['cabinet_id'],)).fetchone()[0]
            return {'item': self.project(db, row, principal), 'inventory_revision': revision, 'generated_at': utcnow()}

    def history(self, principal, identity, limit=20, cursor=None):
        with self.db.connect() as db:
            db.execute('BEGIN')
            row = self._item(db, identity, principal)
            principal.require('history:read')
            revision = db.execute('SELECT inventory_revision FROM cabinets WHERE id=?', (row['cabinet_id'],)).fetchone()[0]
            filters = {'item': identity, 'kind': 'history', 'limit': limit}
            offset = page_offset(cursor, revision, filters)
            changes = db.execute('SELECT c.id,c.action,c.commit_id,c.after_json,t.revision,t.created_at,t.reviewed_by,t.receipt_json '
                'FROM inventory_changes c JOIN inventory_commits t ON t.id=c.commit_id WHERE c.item_id=? '
                'ORDER BY t.revision,c.change_index LIMIT ? OFFSET ?', (identity, limit + 1, offset)).fetchall()
            result = []
            for change in changes[:limit]:
                receipt = json.loads(change['receipt_json'])
                events = [json.loads(r[0]) for r in db.execute('SELECT payload_json FROM verified_events '
                          'WHERE commit_id=? AND item_id=? ORDER BY event_index', (change['commit_id'], identity))]
                baseline = receipt.get('registration_kind') == 'visible_initial_baseline'
                result.append({'id': change['id'], 'action': change['action'], 'inventory_revision': change['revision'],
                    'recorded_at': change['created_at'], 'source': 'initial_baseline' if baseline else
                        'manual' if change['reviewed_by'].startswith('user:') else 'vision',
                    'filmed_inventory_action': bool(events) and not baseline,
                    'events': [{'kind': event['kind'], 'media_window_ms': event.get('event_window_ms')} for event in events],
                    'location_id': json.loads(change['after_json']).get('location_id')})
            return {'item_id': identity, 'inventory_revision': revision, 'history': result,
                'next_cursor': page_cursor(revision, filters, offset + limit) if len(changes) > limit else None}

    def image(self, principal, identity, image_id):
        with self.db.connect() as db:
            self._item(db, identity, principal)
            principal.require('images:read')
            row = db.execute("SELECT o.asset_path FROM observations o JOIN item_observations io ON io.observation_id=o.id "
                             "WHERE io.item_id=? AND o.id=? AND o.kind='crop'", (identity, image_id)).fetchone()
            if not row or not row[0]:
                missing()
            path = (self.db.path.parent / row[0]).resolve()
            if not path.is_relative_to(self.db.assets.resolve()) or not path.is_file():
                missing()
            return path
