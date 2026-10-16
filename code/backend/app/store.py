"""Persistence services for evidence, controlled inventory writes and audit reads."""
import hashlib
import json
from pathlib import Path
import shutil

from .database import Conflict, Database, DatabaseError, NotFound, encode, fingerprint, new_id, timestamp, utcnow
from .inventory_schemas import CommitPlan, ItemValues


def require(connection, table, identity):
    # Table names are application constants, never supplied by API/model input.
    row = connection.execute(f'SELECT * FROM {table} WHERE id = ?', (identity,)).fetchone()
    if row is None:
        raise NotFound(f'{table}: {identity}')
    return dict(row)


def item_value(row):
    values = ItemValues.model_validate(json.loads(row['data_json'])).model_dump(mode='json')
    return {**values, **{k: row[k] for k in
        ('id', 'cabinet_id', 'location_id', 'name', 'category', 'presence_status', 'version', 'created_at', 'updated_at')}}


class Store:
    def __init__(self, database):
        self.db = database if isinstance(database, Database) else Database(database)

    def initialize(self, cabinet_id='cabinet-01', name='Mind Box', known_empty=False):
        self.db.migrate()
        with self.db.transaction() as connection:
            row = connection.execute('SELECT * FROM cabinets WHERE id = ?', (cabinet_id,)).fetchone()
            if row:
                if known_empty and not row['inventory_known']:
                    raise Conflict('Existing unknown inventory cannot be silently marked empty')
            else:
                connection.execute('INSERT INTO cabinets VALUES (?, ?, 0, ?, ?)',
                                   (cabinet_id, name, int(known_empty), utcnow()))
                for number in range(1, 4):
                    connection.execute('INSERT INTO locations VALUES (?, ?, ?)',
                                       (f'{cabinet_id}:drawer-{number}', cabinet_id, f'抽屉{number}'))
        return self.get_inventory_snapshot(cabinet_id)

    def register_device(self, device_id, cabinet_id, role, credential_hash=None):
        if role not in ('camera', 'sensor', 'voice'):
            raise DatabaseError('Unsupported device role')
        with self.db.transaction() as connection:
            require(connection, 'cabinets', cabinet_id)
            row = connection.execute('SELECT * FROM devices WHERE id = ?', (device_id,)).fetchone()
            if row:
                if (row['cabinet_id'], row['role'], row['credential_hash']) != (cabinet_id, role, credential_hash):
                    raise Conflict('Device identity already has different registration')
            else:
                connection.execute('INSERT INTO devices VALUES (?, ?, ?, ?, NULL)',
                                   (device_id, cabinet_id, role, credential_hash))
        return {'device_id': device_id, 'cabinet_id': cabinet_id, 'role': role}

    def ingest_event(self, authenticated_device_id, event):
        allowed = {'event_id', 'device_id', 'boot_id', 'seq', 'kind', 'device_time', 'payload'}
        if set(event) - allowed or not all(event.get(k) for k in ('event_id', 'boot_id', 'kind')):
            raise DatabaseError('Invalid event fields')
        if event.get('device_id') != authenticated_device_id:
            raise DatabaseError('Event device does not match authenticated device')
        if type(event.get('seq')) is not int or event['seq'] < 0 or not isinstance(event.get('payload'), dict):
            raise DatabaseError('Invalid event sequence or payload')
        if event['kind'] not in ('door.opened', 'door.closed', 'heartbeat'):
            raise DatabaseError('Weight events are disabled in the current scope')
        device_time = timestamp(event['device_time']) if event.get('device_time') else None
        body_hash = fingerprint(event)
        with self.db.transaction() as connection:
            device = require(connection, 'devices', authenticated_device_id)
            if event['kind'].startswith('door.') and device['role'] != 'sensor':
                raise DatabaseError('Door events require a sensor device')
            existing = connection.execute('SELECT * FROM events WHERE id = ? OR '
                '(device_id = ? AND boot_id = ? AND seq = ?)',
                (event['event_id'], authenticated_device_id, event['boot_id'], event['seq'])).fetchall()
            if existing:
                if len(existing) != 1 or existing[0]['body_hash'] != body_hash:
                    raise Conflict('Event identity reused with different content')
                return {'event_id': existing[0]['id'], 'duplicate': True}
            now = utcnow()
            connection.execute('INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (event['event_id'], authenticated_device_id, event['boot_id'], event['seq'], event['kind'],
                 device_time, now, encode(event['payload']), body_hash))
            connection.execute('UPDATE devices SET last_seen_at = ? WHERE id = ?', (now, authenticated_device_id))
        return {'event_id': event['event_id'], 'duplicate': False}

    def _archive(self, root, relative):
        root = Path(root).resolve()
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise DatabaseError('Evidence must be an existing file inside the run directory: ' + str(relative))
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        self.db.assets.mkdir(parents=True, exist_ok=True)
        target = self.db.assets / digest
        if target.exists():
            with target.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                    raise DatabaseError('Archived evidence hash mismatch')
        else:
            temporary = self.db.assets / (digest + '.' + new_id() + '.tmp')
            try:
                shutil.copyfile(path, temporary)
                with temporary.open('rb') as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != digest:
                        raise Conflict('Evidence changed while importing')
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return str(target.relative_to(self.db.path.parent)), digest

    def import_run(self, directory, cabinet_id='cabinet-01', operation_id=None,
                   base_revision=None, source_key=None, capture_id=None):
        """Import immutable evidence; no candidate becomes an inventory item here."""
        root = Path(directory).resolve()
        result = json.loads((root / 'result.json').read_text(encoding='utf-8'))
        media_path = root / 'media.json'
        if not media_path.exists() and result.get('status') == 'failed':
            media = {'input_sha256': result.get('input_sha256'), 'frames': [], 'status': 'preparation_failed'}
        else:
            media = json.loads(media_path.read_text(encoding='utf-8'))
        action_path = root / 'action-media.json'
        action = json.loads(action_path.read_text(encoding='utf-8')) if action_path.exists() else {'frames': []}
        artifacts = {}
        for filename in ('action-tool-trace.json', 'tool-trace.json', 'scene-plan.json',
                         'snapshot-states.json', 'window-states.json', 'rejected-state-claims.json'):
            path = root / filename
            if path.exists():
                artifacts[filename] = json.loads(path.read_text(encoding='utf-8'))
        source_key = source_key or str(root)
        inventory_context = result.get('inventory_context')
        if inventory_context is not None:
            if inventory_context['cabinet_id'] != cabinet_id:
                raise Conflict('Analysis used a different cabinet inventory')
            if base_revision is not None and base_revision != inventory_context['inventory_revision']:
                raise Conflict('Import revision does not match the analysis snapshot')
            base_revision = inventory_context['inventory_revision']
        input_hash = result.get('input_sha256')
        if not isinstance(input_hash, str) or len(input_hash) != 64 or any(c not in '0123456789abcdef' for c in input_hash):
            raise DatabaseError('A valid source-video SHA-256 is required')
        if media.get('input_sha256') != input_hash:
            raise Conflict('Media metadata and recognition reference different source videos')
        if operation_id is None:
            with self.db.connect() as connection:
                previous = connection.execute('SELECT o.id FROM operations o JOIN media_captures m ON m.id = o.capture_id '
                    'WHERE o.cabinet_id = ? AND m.input_sha256 = ? ORDER BY o.operation_no LIMIT 1',
                    (cabinet_id, input_hash)).fetchone()
            operation_id = previous['id'] if previous else 'offline-' + fingerprint([cabinet_id, input_hash])[:32]
        capture_id = capture_id or 'capture-' + fingerprint([cabinet_id, operation_id])[:32]
        frames = {}
        tool_frames = [frame for entry in artifacts.get('action-tool-trace.json', {}).get('trace', [])
                       if entry.get('tool') == 'get_frames' and entry.get('result', {}).get('status') == 'ok'
                       for frame in entry['result'].get('frames', [])]
        for frame in media.get('frames', []) + action.get('frames', []) + tool_frames:
            if frame['id'] in frames:
                for key in ('file', 'media_time_ms', 'pts'):
                    if frame.get(key) != frames[frame['id']].get(key):
                        raise Conflict('Frame identity has conflicting media coordinates')
            frames[frame['id']] = frame
        records = []
        for kind, entries in (('frame', frames), ('crop', result.get('evidence', {}))):
            for local_ref, evidence in entries.items():
                if not isinstance(evidence.get('media_time_ms'), int) or evidence['media_time_ms'] < 0:
                    raise DatabaseError('Evidence requires nonnegative integer media_time_ms')
                if kind == 'crop' and evidence.get('frame_id') not in frames:
                    raise DatabaseError('Crop references an unknown frame')
                asset, digest = self._archive(root, evidence['file'])
                records.append((local_ref, kind, asset, digest, evidence))
        if len({record[0] for record in records}) != len(records):
            raise Conflict('Frame and crop identities must be distinct')
        known_refs = {record[0] for record in records}
        candidates = result.get('items', [])
        candidate_ids = {c['candidate_id'] for c in candidates}
        if len(candidate_ids) != len(candidates):
            raise Conflict('Duplicate candidate identities')
        for candidate in candidates:
            if not candidate.get('evidence_refs') or not set(candidate['evidence_refs']) <= known_refs:
                raise DatabaseError('Candidate references unknown evidence')
        analysis = result.get('operation_analysis', {})
        net = analysis.get('net_changes', [])
        net_ids = [entry['candidate_id'] for entry in net]
        if len(net_ids) != len(set(net_ids)) or not set(net_ids) <= candidate_ids:
            raise DatabaseError('Net changes contain duplicate or unknown candidate identities')
        events = analysis.get('event_candidates', result.get('event_candidates', []))
        for event in events:
            if not set(event['candidate_ids']) <= candidate_ids:
                raise DatabaseError('Action references unknown candidates')
            if not {event['before_frame_id'], event['after_frame_id']} <= known_refs:
                raise DatabaseError('Action references missing frames')
        pending = list(analysis.get('pending', [])) + list(result.get('possible_duplicate_links', []))
        if result.get('status') != 'completed' or analysis.get('status') != 'consistent':
            pending.append({'reason': 'analysis_not_consistent', 'status': analysis.get('status', 'missing')})
        content_hash = fingerprint({'result': result, 'media': media, 'action': action, 'artifacts': artifacts,
                                    'assets': [(r[0], r[3]) for r in records]})
        with self.db.transaction() as connection:
            cabinet = require(connection, 'cabinets', cabinet_id)
            existing_op = connection.execute('SELECT * FROM operations WHERE id = ?', (operation_id,)).fetchone()
            if existing_op and existing_op['cabinet_id'] != cabinet_id:
                raise Conflict('Operation belongs to another cabinet')
            existing = connection.execute('SELECT * FROM processing_runs WHERE operation_id = ? AND source_key = ?',
                                          (operation_id, source_key)).fetchone()
            if existing:
                if existing['body_hash'] != content_hash:
                    raise Conflict('Imported run changed; use a new source key/attempt')
                if base_revision is not None and existing['base_revision'] != base_revision:
                    raise Conflict('Imported run already uses another inventory revision')
                return {'run_id': existing['id'], 'operation_id': operation_id, 'duplicate': True,
                        'base_revision': existing['base_revision']}
            if existing_op and existing_op['state'] == 'committed':
                raise Conflict('Committed operations are immutable; create a correction operation')
            if base_revision is None:
                base_revision = cabinet['inventory_revision']
            if type(base_revision) is not int or not 0 <= base_revision <= cabinet['inventory_revision']:
                raise DatabaseError('Invalid base inventory revision')
            capture = connection.execute('SELECT * FROM media_captures WHERE id = ?', (capture_id,)).fetchone()
            if capture and (capture['cabinet_id'] != cabinet_id or capture['input_sha256'] != input_hash):
                raise Conflict('Capture identity conflicts with source media')
            if existing_op and existing_op['capture_id'] != capture_id:
                raise Conflict('An attempt cannot change its operation capture')
            if not capture:
                connection.execute('INSERT INTO media_captures VALUES (?, ?, ?, ?, NULL, ?)',
                    (capture_id, cabinet_id, input_hash, result['video'], encode(media)))
                connection.execute('INSERT INTO media_segments VALUES (?, ?, 0, ?, ?)',
                    (new_id(), capture_id, result['video'], input_hash))
            now, run_id = utcnow(), new_id()
            attempt = existing_op['current_attempt'] + 1 if existing_op else 1
            state = 'failed' if result.get('status') == 'failed' else 'pending' if pending else 'ready'
            if not existing_op:
                number = connection.execute('SELECT COALESCE(MAX(operation_no), 0) + 1 FROM operations '
                                            'WHERE cabinet_id = ?', (cabinet_id,)).fetchone()[0]
                connection.execute('INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?, ?, NULL)',
                                   (operation_id, cabinet_id, capture_id, number, attempt, state, now))
            else:
                connection.execute('UPDATE operations SET current_attempt = ?, state = ? WHERE id = ?',
                                   (attempt, state, operation_id))
            status = result.get('status', 'failed')
            connection.execute('INSERT INTO processing_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (run_id, operation_id, attempt, source_key, content_hash, base_revision, status,
                 analysis.get('status', 'missing'), encode(result), now, encode(artifacts)))
            for ref, kind, asset, digest, evidence in records:
                connection.execute('INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (new_id(), run_id, ref, kind, asset, digest, evidence['media_time_ms'], encode(evidence)))
            for candidate in candidates:
                connection.execute('INSERT INTO candidates VALUES (?, ?, ?)',
                                   (run_id, candidate['candidate_id'], encode(candidate)))
            for entry in pending:
                connection.execute('INSERT INTO pending_candidates VALUES (?, ?, ?, NULL, NULL, NULL)',
                                   (new_id(), run_id, encode(entry)))
        return {'run_id': run_id, 'operation_id': operation_id, 'base_revision': base_revision,
                'state': state, 'observation_count': len(records), 'pending_count': len(pending), 'duplicate': False}

    def get_inventory_snapshot(self, cabinet_id='cabinet-01', include_removed=False, query=None, location_id=None):
        with self.db.connect() as connection:
            connection.execute('BEGIN')
            cabinet = require(connection, 'cabinets', cabinet_id)
            conditions, parameters = ['cabinet_id = ?'], [cabinet_id]
            if not include_removed:
                conditions.append("presence_status != 'removed'")
            if query is not None:
                conditions.append('(instr(lower(name), lower(?)) > 0 OR instr(lower(category), lower(?)) > 0)')
                parameters.extend([query, query])
            if location_id is not None:
                conditions.append('location_id = ?')
                parameters.append(location_id)
            rows = connection.execute('SELECT * FROM items WHERE ' + ' AND '.join(conditions) + ' ORDER BY created_at, id', parameters)
            items = [item_value(row) for row in rows]
            unresolved = connection.execute('SELECT COUNT(*) FROM pending_candidates p JOIN processing_runs r '
                'ON r.id = p.run_id JOIN operations o ON o.id = r.operation_id '
                'WHERE o.cabinet_id = ? AND p.resolved_at IS NULL AND r.attempt_no = o.current_attempt',
                (cabinet_id,)).fetchone()[0]
            return {'cabinet_id': cabinet_id, 'inventory_revision': cabinet['inventory_revision'],
                    'inventory_known': bool(cabinet['inventory_known']), 'items': items, 'pending_count': unresolved,
                    'completeness': 'known_baseline' if cabinet['inventory_known'] else 'unknown_baseline'}

    def get_item(self, item_id):
        with self.db.connect() as connection:
            item = item_value(require(connection, 'items', item_id))
            item['observations'] = [dict(r) for r in connection.execute('SELECT o.* FROM observations o '
                'JOIN item_observations io ON io.observation_id = o.id WHERE io.item_id = ?', (item_id,))]
            item['storage_visits'] = [dict(r) for r in connection.execute('SELECT * FROM storage_visits WHERE item_id = ?', (item_id,))]
            return item

    def get_operation(self, operation_id):
        with self.db.connect() as connection:
            operation = require(connection, 'operations', operation_id)
            operation['runs'] = [dict(r) for r in connection.execute('SELECT id, attempt_no, base_revision, status, analysis_status '
                'FROM processing_runs WHERE operation_id = ? ORDER BY attempt_no', (operation_id,))]
            operation['receipts'] = [json.loads(r[0]) for r in connection.execute(
                'SELECT receipt_json FROM inventory_commits WHERE operation_id = ?', (operation_id,))]
            return operation

    def resolve_pending(self, run_id, resolution, reviewed_by):
        if not resolution.strip() or not reviewed_by.strip():
            raise DatabaseError('Resolution and reviewer are required')
        with self.db.transaction() as connection:
            run = require(connection, 'processing_runs', run_id)
            operation = require(connection, 'operations', run['operation_id'])
            if operation['state'] == 'committed' or run['attempt_no'] != operation['current_attempt']:
                raise Conflict('Only the current uncommitted attempt can be reviewed')
            if run['status'] != 'completed':
                raise Conflict('Failed/incomplete analysis must be rerun')
            connection.execute('UPDATE pending_candidates SET resolution = ?, resolved_by = ?, resolved_at = ? '
                'WHERE run_id = ? AND resolved_at IS NULL', (resolution, reviewed_by, utcnow(), run_id))
            connection.execute("UPDATE operations SET state = 'ready' WHERE id = ?", (operation['id'],))

    def _start_commit(self, connection, cabinet_id, key, body, revision, reviewer, operation_id=None, run_id=None):
        if not key or not key.strip() or not reviewer or not reviewer.strip():
            raise DatabaseError('Idempotency key and reviewer are required')
        digest = fingerprint(body)
        existing = connection.execute('SELECT * FROM inventory_commits WHERE cabinet_id = ? AND idempotency_key = ?',
                                      (cabinet_id, key)).fetchone()
        if existing:
            if existing['body_hash'] != digest:
                raise Conflict('Idempotency key reused with a different request')
            return json.loads(existing['receipt_json']), True
        cabinet = require(connection, 'cabinets', cabinet_id)
        if type(revision) is not int or cabinet['inventory_revision'] != revision:
            raise Conflict('Inventory revision changed; reload and review the plan')
        changed = connection.execute('UPDATE cabinets SET inventory_revision = inventory_revision + 1 '
            'WHERE id = ? AND inventory_revision = ?', (cabinet_id, revision)).rowcount
        if changed != 1:
            raise Conflict('Inventory revision changed')
        receipt = {'status': 'committed', 'commit_id': new_id(), 'cabinet_id': cabinet_id,
            'operation_id': operation_id, 'run_id': run_id, 'revision': revision + 1,
            'added_item_ids': [], 'removed_item_ids': [], 'updated_item_ids': [], 'kept_item_ids': [], 'evidence_refs': []}
        connection.execute('INSERT INTO inventory_commits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (receipt['commit_id'], cabinet_id, operation_id, run_id, revision + 1, key, digest, reviewer, encode(receipt), utcnow()))
        return receipt, False

    def _write_item(self, connection, cabinet_id, values, item_id=None, expected_version=None):
        values = ItemValues.model_validate(values).model_dump(mode='json')
        if values['location_id']:
            location = require(connection, 'locations', values['location_id'])
            if location['cabinet_id'] != cabinet_id:
                raise DatabaseError('Location belongs to another cabinet')
        now, before = utcnow(), None
        if item_id:
            before = item_value(require(connection, 'items', item_id))
            if before['cabinet_id'] != cabinet_id:
                raise DatabaseError('Item belongs to another cabinet')
            if expected_version != before['version']:
                raise Conflict('Item version changed')
            version = before['version'] + 1
            connection.execute('UPDATE items SET location_id = ?, name = ?, category = ?, presence_status = ?, '
                'version = ?, data_json = ?, updated_at = ? WHERE id = ?',
                (values['location_id'], values['name'], values['category'], values['presence_status'], version, encode(values), now, item_id))
        else:
            item_id, version = new_id(), 1
            connection.execute('INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (item_id, cabinet_id, values['location_id'], values['name'], values['category'], values['presence_status'],
                 version, encode(values), now, now))
        return before, item_value(require(connection, 'items', item_id))

    def _change(self, connection, receipt, action, before, after, refs, index):
        connection.execute('INSERT INTO inventory_changes VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (new_id(), receipt['commit_id'], after['id'], action, encode(before) if before else None,
             encode(after), encode(refs), index))
        bucket = 'added_item_ids' if action == 'add' else 'removed_item_ids' if action == 'remove' else (
            'kept_item_ids' if action == 'keep' else 'updated_item_ids')
        receipt[bucket].append(after['id'])
        receipt['evidence_refs'].extend(ref for ref in refs if ref not in receipt['evidence_refs'])

    def _visit(self, connection, item_id, inside, operation_id, refs, at=None):
        current = connection.execute('SELECT id FROM storage_visits WHERE item_id = ? AND closed = 0', (item_id,)).fetchone()
        if inside and not current:
            connection.execute('INSERT INTO storage_visits VALUES (?, ?, ?, NULL, ?, NULL, ?, ?, 0)',
                               (new_id(), item_id, operation_id, at, encode(refs), encode([])))
        elif not inside and current:
            connection.execute('UPDATE storage_visits SET closed = 1, exited_operation_id = ?, exited_at = ?, '
                               'exit_evidence_json = ? WHERE id = ?', (operation_id, at, encode(refs), current['id']))

    def _finish_commit(self, connection, receipt):
        connection.execute('UPDATE inventory_commits SET receipt_json = ? WHERE id = ?', (encode(receipt), receipt['commit_id']))
        connection.execute('INSERT INTO jobs (id, business_key, kind, payload_json, next_run_at) VALUES (?, ?, ?, ?, ?)',
            (new_id(), 'inventory:' + receipt['commit_id'], 'inventory.changed', encode(receipt), utcnow()))
        return receipt

    def create_item(self, cabinet_id, values, base_revision, idempotency_key, reviewed_by):
        values = dict(values)
        meta = dict(values.get('field_meta', {}))
        for key, value in values.items():
            if key != 'field_meta' and key not in meta:
                meta[key] = {'source': 'user', 'certainty': 'recorded' if value is not None else 'unknown',
                             'evidence_refs': [], 'extractor': reviewed_by}
        values['field_meta'] = meta
        values = ItemValues.model_validate(values).model_dump(mode='json')
        body = {'kind': 'manual.add', 'values': values, 'base_revision': base_revision, 'reviewed_by': reviewed_by}
        with self.db.transaction() as connection:
            receipt, duplicate = self._start_commit(connection, cabinet_id, idempotency_key, body, base_revision, reviewed_by)
            if duplicate:
                return receipt
            before, after = self._write_item(connection, cabinet_id, values)
            self._change(connection, receipt, 'add', before, after, [], 0)
            if after['presence_status'] != 'removed':
                self._visit(connection, after['id'], True, None, [], after['stored_at'])
            return self._finish_commit(connection, receipt)

    def correct_item(self, cabinet_id, item_id, patch, expected_version, base_revision, idempotency_key, reviewed_by):
        if set(patch) - set(ItemValues.model_fields):
            raise DatabaseError('Only business item fields may be corrected')
        body = {'kind': 'manual.correct', 'item_id': item_id, 'patch': patch, 'expected_version': expected_version,
                'base_revision': base_revision, 'reviewed_by': reviewed_by}
        with self.db.transaction() as connection:
            receipt, duplicate = self._start_commit(connection, cabinet_id, idempotency_key, body, base_revision, reviewed_by)
            if duplicate:
                return receipt
            before = item_value(require(connection, 'items', item_id))
            values = {k: before[k] for k in ItemValues.model_fields}
            values.update(patch)
            meta = {**before['field_meta'], **patch.get('field_meta', {})}
            for key, value in patch.items():
                if key != 'field_meta':
                    meta[key] = {'source': 'user', 'certainty': 'recorded' if value is not None else 'unknown',
                                 'evidence_refs': [], 'extractor': reviewed_by}
            values['field_meta'] = meta
            before, after = self._write_item(connection, cabinet_id, values, item_id, expected_version)
            action = 'remove' if after['presence_status'] == 'removed' and before['presence_status'] != 'removed' else (
                'return' if before['presence_status'] == 'removed' and after['presence_status'] == 'present' else 'update')
            self._change(connection, receipt, action, before, after, [], 0)
            if after['presence_status'] in ('present', 'removed'):
                self._visit(connection, item_id, after['presence_status'] == 'present', None, [], None)
            return self._finish_commit(connection, receipt)

    def commit_inventory(self, plan):
        plan = CommitPlan.model_validate(plan)
        body = plan.model_dump(mode='json')
        with self.db.transaction() as connection:
            operation = require(connection, 'operations', plan.operation_id)
            cabinet_id = operation['cabinet_id']
            existing = connection.execute('SELECT * FROM inventory_commits WHERE cabinet_id = ? AND idempotency_key = ?',
                                          (cabinet_id, plan.idempotency_key)).fetchone()
            if existing:
                if existing['body_hash'] != fingerprint(body):
                    raise Conflict('Idempotency key reused with a different request')
                return json.loads(existing['receipt_json'])
            run = require(connection, 'processing_runs', plan.run_id)
            if run['operation_id'] != plan.operation_id or run['attempt_no'] != operation['current_attempt']:
                raise Conflict('Analysis attempt is not current for this operation')
            if operation['state'] != 'ready' or run['status'] != 'completed':
                raise Conflict('Operation must be ready; unresolved/failed candidates cannot be committed')
            if run['base_revision'] != plan.base_revision:
                raise Conflict('Plan does not match the analysis inventory revision')
            if connection.execute('SELECT 1 FROM pending_candidates WHERE run_id = ? AND resolved_at IS NULL', (plan.run_id,)).fetchone():
                raise Conflict('Resolve pending candidates before committing')
            result = json.loads(run['result_json'])
            analysis = result.get('operation_analysis', {})
            candidates = {r['candidate_id']: json.loads(r['payload_json']) for r in connection.execute(
                'SELECT * FROM candidates WHERE run_id = ?', (plan.run_id,))}
            observations = {r['local_ref']: dict(r) for r in connection.execute('SELECT * FROM observations WHERE run_id = ?', (plan.run_id,))}
            net = {n['candidate_id']: n['change'] for n in analysis.get('net_changes', [])}
            events = analysis.get('event_candidates', [])
            needed = {n['candidate_id'] for n in analysis.get('net_changes', []) if n['change'] in ('add', 'remove')}
            needed.update(cid for e in events for cid in e['candidate_ids'])
            decisions = {d.candidate_id: d for d in plan.decisions}
            if not needed <= decisions.keys():
                raise DatabaseError('Plan omits net changes or intermediate event identities')
            receipt, _ = self._start_commit(connection, cabinet_id, plan.idempotency_key, body,
                plan.base_revision, plan.reviewed_by, plan.operation_id, plan.run_id)
            for index, decision in enumerate(plan.decisions):
                cid, action = decision.candidate_id, decision.action
                if cid not in candidates or not set(decision.evidence_refs) <= observations.keys():
                    raise DatabaseError('Unknown candidate or evidence reference')
                candidate = candidates[cid]
                candidate_refs = set(candidate['evidence_refs'])
                event_refs = {e[k] for e in events if cid in e['candidate_ids'] for k in ('before_frame_id', 'after_frame_id')}
                if not set(decision.evidence_refs) <= candidate_refs | event_refs:
                    raise DatabaseError('Evidence is not associated with this candidate')
                expected = {'add': ('add',), 'remove': ('remove',), 'return': ('add',),
                            'update': ('kept',), 'keep': ('kept', 'not_present')}
                if net.get(cid) not in expected[action]:
                    raise DatabaseError('Decision disagrees with the observed net change')
                if set(decision.patch) - set(ItemValues.model_fields):
                    raise DatabaseError('Invalid item patch field')
                before = None
                if action == 'add':
                    values = {k: candidate[k] for k in ItemValues.model_fields if k in candidate}
                    if plan.coarse_only:
                        if decision.patch:
                            raise DatabaseError('Coarse insertion cannot apply detailed identity patches')
                        category = candidate.get('category', '未知')
                        values = {'name': '饮品' if category == '饮品' else '未知食品' if category == '食品' else '未知物品',
                                  'category': category, 'product_match_status': 'category_only',
                                  'field_meta': {}}
                    values['presence_status'] = 'present'
                else:
                    before = item_value(require(connection, 'items', decision.item_id))
                    if before['cabinet_id'] != cabinet_id or before['version'] != decision.expected_version:
                        raise Conflict('Item identity, ownership or version changed')
                    if action == 'remove' and before['presence_status'] not in ('present', 'unseen'):
                        raise DatabaseError('Only an existing in-cabinet item can be removed')
                    if action == 'return' and before['presence_status'] != 'removed':
                        raise DatabaseError('A return must reference the previously removed physical item')
                    if action in ('keep', 'update') and ((net[cid] == 'kept') != (before['presence_status'] in ('present', 'unseen'))):
                        raise DatabaseError('Kept item presence disagrees with inventory')
                    values = {k: before[k] for k in ItemValues.model_fields}
                    if action in ('return', 'remove'):
                        values['presence_status'] = 'present' if action == 'return' else 'removed'
                    if action == 'return':
                        values['stored_at'] = None
                values.update(decision.patch)
                meta = {**values.get('field_meta', {})}
                for key, value in decision.patch.items():
                    if key != 'field_meta':
                        meta[key] = {'source': 'user', 'certainty': 'recorded' if value is not None else 'unknown',
                                     'evidence_refs': [], 'extractor': plan.reviewed_by}
                if action == 'return' and 'stored_at' not in decision.patch:
                    meta['stored_at'] = {'source': 'system', 'certainty': 'unknown', 'evidence_refs': []}
                if action == 'add':
                    for key in ('first_seen_at', 'last_seen_at', 'stored_at', 'opened_at'):
                        if values.get(key) is not None and key not in decision.patch:
                            raise DatabaseError('No trusted capture clock anchor; absolute observation times require explicit correction')
                for key, value in values.items():
                    if key != 'field_meta' and value is not None and key not in meta:
                        meta[key] = {'source': 'vision' if action == 'add' else 'system',
                                     'certainty': 'estimated', 'evidence_refs': decision.evidence_refs}
                for field, entry in meta.items():
                    entry = dict(entry)
                    refs = entry.get('evidence_refs', [])
                    # Existing item metadata already contains stable database observation IDs.
                    if action == 'add':
                        if not set(refs) <= observations.keys():
                            raise DatabaseError('Field provenance references unknown evidence')
                        entry['evidence_refs'] = [observations[ref]['id'] for ref in refs]
                    meta[field] = entry
                values['field_meta'] = meta
                target_presence = 'present' if action in ('add', 'return') else 'removed' if action == 'remove' else None
                if action == 'update':
                    target_presence = before['presence_status']
                if target_presence and values['presence_status'] != target_presence:
                    raise DatabaseError('Patch cannot contradict the net action')
                if action == 'keep' and decision.patch:
                    raise DatabaseError('Use update for corrections to a kept item')
                if action == 'keep':
                    after = before
                else:
                    before, after = self._write_item(connection, cabinet_id, values, decision.item_id, decision.expected_version)
                all_refs = sorted(set(decision.evidence_refs) | candidate_refs | event_refs)
                observation_ids = [observations[ref]['id'] for ref in all_refs]
                for obs_id in observation_ids:
                    connection.execute('INSERT OR IGNORE INTO item_observations VALUES (?, ?)', (after['id'], obs_id))
                self._change(connection, receipt, action, before, after, observation_ids, index)
                # Preserve intermediate exits/returns separately from the single net write.
                for event_index, event in enumerate(events):
                    if cid not in event['candidate_ids']:
                        continue
                    refs = [observations[event[k]]['id'] for k in ('before_frame_id', 'after_frame_id')]
                    connection.execute('INSERT INTO verified_events VALUES (?, ?, ?, ?, ?)',
                        (new_id(), receipt['commit_id'], after['id'], event_index,
                         encode({**event, 'committed': True, 'evidence_refs': refs})))
                    if event['kind'] in ('add', 'return', 'remove'):
                        self._visit(connection, after['id'], event['kind'] != 'remove', plan.operation_id, refs)
                self._visit(connection, after['id'], after['presence_status'] != 'removed', plan.operation_id, observation_ids,
                            after.get('stored_at') if action == 'add' else None)
            connection.execute("UPDATE operations SET state = 'committed', committed_at = ? WHERE id = ?", (utcnow(), plan.operation_id))
            return self._finish_commit(connection, receipt)

    def receipts(self, cabinet_id):
        with self.db.connect() as connection:
            return [json.loads(r[0]) for r in connection.execute('SELECT receipt_json FROM inventory_commits '
                'WHERE cabinet_id = ? ORDER BY revision', (cabinet_id,))]

    def history(self, item_id):
        with self.db.connect() as connection:
            require(connection, 'items', item_id)
            return [dict(r) for r in connection.execute('SELECT c.*, t.revision, t.reviewed_by, t.created_at '
                'FROM inventory_changes c JOIN inventory_commits t ON t.id = c.commit_id '
                'WHERE c.item_id = ? ORDER BY t.revision, c.change_index', (item_id,))]
