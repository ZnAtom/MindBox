"""Small verified product memory; lexical relevance never proves physical presence."""
import base64
import hashlib
import json
from pathlib import Path
import re

from .client import write_json
from .database import Conflict, DatabaseError, encode, fingerprint, new_id, utcnow
from .enrichment import Enrichment
from .inventory_schemas import ItemValues
from .store import require
from .tools import tokens

PRODUCT_FIELDS = {'name', 'category', 'description', 'brand', 'variant'}


def validate_fields(fields):
    if set(fields) - PRODUCT_FIELDS or not fields.get('name') or not fields.get('category'):
        raise DatabaseError('Product memory only accepts reusable identity fields')
    return {key: value for key, value in ItemValues.model_validate(fields).model_dump(mode='json').items()
            if key in fields}


def image_hash(content):
    from io import BytesIO
    from PIL import Image
    with Image.open(BytesIO(content)) as image:
        pixels = list(image.convert('L').resize((9, 8)).get_flattened_data())
    bits = sum((pixels[y * 9 + x] > pixels[y * 9 + x + 1]) << (y * 8 + x) for y in range(8) for x in range(8))
    return f'{bits:016x}'


def valid_barcode(value):
    if not value or not re.fullmatch(r'(?:\d{8}|\d{12,14})', value):
        return False
    return (sum(int(n) * (3 if i % 2 == 0 else 1) for i, n in enumerate(reversed(value[:-1]))) + int(value[-1])) % 10 == 0


class ProductMemory:
    def __init__(self, context, cabinet_id='cabinet-01'):
        self.context, self.db, self.cabinet_id = context, context.store.db, cabinet_id

    def stage(self, item_id, fields, aliases=(), barcode=None):
        fields = validate_fields(fields)
        if len(aliases) > 20 or any(not isinstance(a, str) or len(a) > 120 for a in aliases):
            raise DatabaseError('Product aliases must be bounded text')
        if barcode and not valid_barcode(barcode):
            raise DatabaseError('Invalid GS1 barcode checksum')
        now, identity = utcnow(), new_id()
        with self.db.transaction() as connection:
            item = require(connection, 'items', item_id)
            if item['cabinet_id'] != self.cabinet_id:
                raise DatabaseError('Product source belongs to another cabinet')
            evidence = [dict(r) for r in connection.execute(
                'SELECT o.*, m.input_sha256 FROM observations o JOIN item_observations io ON io.observation_id=o.id '
                'JOIN processing_runs r ON r.id=o.run_id JOIN operations p ON p.id=r.operation_id '
                'JOIN media_captures m ON m.id=p.capture_id WHERE io.item_id=? AND o.kind=\'crop\'', (item_id,))]
            if not evidence:
                raise DatabaseError('Product learning requires associated object crops')
            connection.execute('INSERT INTO product_profiles VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)',
                (identity, self.cabinet_id, 'candidate', encode(fields), encode(list(aliases)), barcode,
                 encode({'source_item': item_id, 'status': 'unverified'}), now, now))
            for row in evidence:
                connection.execute('INSERT OR IGNORE INTO product_examples VALUES (?, ?, ?, ?, ?, ?)',
                    (new_id(), identity, row['asset_path'], row['sha256'], row['input_sha256'],
                     encode({'observation_id': row['id'], 'media_time_ms': row['media_time_ms']})))
            for key, value in fields.items():
                connection.execute('INSERT INTO product_claims VALUES (?, ?, ?, ?, ?)',
                    (new_id(), identity, key, encode(value), encode({'source_item': item_id,
                     'observations': [r['id'] for r in evidence], 'status': 'candidate'})))
        return self.get(identity)

    def get(self, product_id):
        with self.db.connect() as connection:
            row = require(connection, 'product_profiles', product_id)
            if row['cabinet_id'] != self.cabinet_id:
                raise DatabaseError('Product belongs to another cabinet')
            return {**row, 'fields': json.loads(row['fields_json']), 'aliases': json.loads(row['aliases_json']),
                    'verification': json.loads(row['verification_json'])}

    def verify(self, product_id, reviewed_by, reason):
        if not reviewed_by.strip() or not reason.strip():
            raise DatabaseError('Explicit evidence review is required to promote product memory')
        with self.db.transaction() as connection:
            row = require(connection, 'product_profiles', product_id)
            if row['cabinet_id'] != self.cabinet_id or row['status'] != 'candidate':
                raise Conflict('Only an in-scope candidate can be promoted')
            verification = {**json.loads(row['verification_json']), 'reviewed_by': reviewed_by,
                            'reason': reason, 'reviewed_at': utcnow(), 'status': 'verified'}
            connection.execute("UPDATE product_profiles SET status='verified', verification_json=?, updated_at=? WHERE id=?",
                               (encode(verification), utcnow(), product_id))
        return self.get(product_id)

    def retract(self, product_id, reason):
        if not reason.strip():
            raise DatabaseError('Retraction reason required')
        with self.db.transaction() as connection:
            row = require(connection, 'product_profiles', product_id)
            if row['cabinet_id'] != self.cabinet_id:
                raise DatabaseError('Wrong product scope')
            connection.execute("UPDATE product_profiles SET status='rejected', version=version+1, updated_at=?, "
                'verification_json=? WHERE id=?', (utcnow(), encode({'retraction': reason}), product_id))
        return {'status': 'retracted', 'product_id': product_id,
                'affected_items': self.linked_items(product_id), 'note': 'Existing item fields require revalidation; never silently overwritten.'}

    def linked_items(self, product_id):
        with self.db.connect() as connection:
            return [r[0] for r in connection.execute('SELECT item_id FROM item_product_links WHERE product_id=?', (product_id,))]

    def search(self, query, limit=3, barcode=None):
        if not isinstance(query, str) or type(limit) is not int or not 1 <= limit <= 3:
            raise DatabaseError('Search accepts text and at most three candidates')
        snapshot = self.snapshot()['content_hash']
        result = {'scope_id': self.context.manifest['scope_id'], 'mode': self.context.manifest['mode'],
                  'snapshot': snapshot, 'results': [], 'score_meaning': 'lexical relevance, not probability'}
        if self.context.manifest['mode'] == 'disabled':
            return {**result, 'status': 'disabled'}
        if barcode is not None and not valid_barcode(barcode):
            raise DatabaseError('Barcode query checksum is invalid')
        query_terms = tokens(query[:500])
        with self.db.connect() as connection:
            rows = connection.execute("SELECT * FROM product_profiles WHERE cabinet_id=? AND status='verified'", (self.cabinet_id,))
            for row in rows:
                fields = json.loads(row['fields_json'])
                terms = tokens(' '.join(str(v) for v in fields.values() if v) + ' '.join(json.loads(row['aliases_json'])))
                exact = bool(barcode and barcode == row['barcode'])
                score = len(query_terms & terms) / max(1, len(query_terms))
                if barcode and not exact:
                    continue
                if exact or score > 0:
                    result['results'].append({'product_id': row['id'], 'version': row['version'], 'fields': fields,
                        'score': round(score, 4), 'match_type': 'barcode' if exact else 'lexical',
                        'reference': f"rag:{row['id']}:{row['version']}"})
        result['results'] = sorted(result['results'], key=lambda r: (-int(r['match_type'] == 'barcode'), -r['score'], r['product_id']))[:limit]
        return {**result, 'status': 'ok'}

    def reuse(self, item_id, candidate, observed, expected_version, base_revision, key):
        with self.db.connect() as connection:
            duplicate = connection.execute('SELECT 1 FROM inventory_commits WHERE cabinet_id=? AND idempotency_key=?',
                                           (self.cabinet_id, key)).fetchone()
        if duplicate:
            fields = validate_fields(candidate['fields'])
            patch = {**fields, 'product_id': candidate['product_id'], 'product_match_status': 'verified'}
            provenance = {field: {'source': 'rag', 'certainty': 'recorded',
                'evidence_refs': [candidate['reference']], 'extractor': 'verified-memory-v1'} for field in patch}
            return Enrichment(self.context.store).apply(item_id, patch, provenance, expected_version, base_revision, key,
                                                        cabinet_id=self.cabinet_id)
        current = self.get(candidate['product_id'])
        if current['status'] != 'verified' or current['version'] != candidate['version']:
            raise Conflict('Retrieved product was changed or retracted')
        if self.context.manifest['mode'] == 'disabled':
            raise DatabaseError('Product reuse is disabled in this experiment')
        reference = f"rag:{current['id']}:{current['version']}"
        if candidate.get('reference') != reference:
            raise DatabaseError('Retrieved reference does not match the current product')
        item = self.context.store.get_item(item_id)
        if item['cabinet_id'] != self.cabinet_id or item['version'] != expected_version:
            raise Conflict('Observed physical item changed')
        for field in ('brand', 'variant'):
            if observed.get(field) != item.get(field):
                raise DatabaseError('Match attributes must come from the current item record')
        identity_sources = [item['field_meta'].get(f, {}) for f in ('brand', 'variant')]
        grounded = all(meta.get('source') in ('vision', 'ocr', 'barcode', 'user') and
                       (meta.get('evidence_refs') or meta.get('source') == 'user') for meta in identity_sources)
        fields = current['fields']
        if any(value and fields.get(field) and value != fields[field] for field, value in observed.items() if field in ('brand', 'variant')):
            return {'status': 'rejected', 'reason': 'Observed brand or variant conflicts with memory'}
        # A caller-supplied barcode or match label is not decoding evidence.
        decoded = [code for o in item['observations'] if o['kind'] == 'crop'
                   for code in json.loads(o['metadata_json']).get('decoded_barcodes', [])]
        exact = valid_barcode(current['barcode']) and current['barcode'] in decoded
        discriminating = bool(grounded and fields.get('brand') and observed.get('brand') == fields['brand'] and
                              fields.get('variant') and observed.get('variant') == fields['variant'])
        if not exact and not discriminating:
            return {'status': 'candidate', 'reason': 'Category/name similarity does not establish the same product',
                    'product_id': current['id']}
        patch = {**fields, 'product_id': current['id'], 'product_match_status': 'verified'}
        meta = {key: {'source': 'rag', 'certainty': 'recorded', 'evidence_refs': [candidate['reference']],
                      'extractor': 'verified-memory-v1'} for key in patch}
        return Enrichment(self.context.store).apply(item_id, patch, meta, expected_version, base_revision, key,
                                                    cabinet_id=self.cabinet_id)

    def snapshot(self, output=None):
        products = []
        with self.db.connect() as connection:
            for row in connection.execute("SELECT * FROM product_profiles WHERE cabinet_id=? AND status='verified' ORDER BY id", (self.cabinet_id,)):
                examples = []
                for example in connection.execute('SELECT * FROM product_examples WHERE product_id=? ORDER BY sha256', (row['id'],)):
                    asset = (self.db.path.parent / example['asset_path']).resolve()
                    if self.db.assets.resolve() not in asset.parents:
                        raise DatabaseError('Product example escaped the asset directory')
                    content = asset.read_bytes()
                    if hashlib.sha256(content).hexdigest() != example['sha256']:
                        raise Conflict('Product image checksum mismatch')
                    examples.append({'sha256': example['sha256'], 'capture_hash': example['capture_hash'],
                                     'dhash': image_hash(content),
                                     'metadata': json.loads(example['metadata_json']),
                                     'image_b64': base64.b64encode(content).decode('ascii')})
                claims = [{'field': c['field_name'], 'value': json.loads(c['value_json']),
                           'provenance': json.loads(c['provenance_json'])} for c in connection.execute(
                    'SELECT * FROM product_claims WHERE product_id=? ORDER BY field_name', (row['id'],))]
                products.append({'id': row['id'], 'version': row['version'], 'fields': json.loads(row['fields_json']),
                    'aliases': json.loads(row['aliases_json']), 'barcode': row['barcode'],
                    'verification': json.loads(row['verification_json']), 'examples': examples, 'claims': claims})
        snapshot = {'schema': 1, 'products': products, 'content_hash': fingerprint(products)}
        with self.db.transaction() as connection:
            connection.execute('INSERT OR IGNORE INTO knowledge_snapshots VALUES (?, ?, ?, ?)',
                               (snapshot['content_hash'], snapshot['content_hash'], encode(snapshot), utcnow()))
        if output:
            write_json(output, snapshot)
        return snapshot

    def import_snapshot(self, snapshot, excluded=(), target_hashes=()):
        if self.context.manifest['mode'] not in ('warm', 'holdout'):
            raise DatabaseError('Memory seeds require an explicit warm/holdout context')
        if fingerprint(snapshot['products']) != snapshot['content_hash']:
            raise Conflict('Snapshot checksum mismatch')
        blocked = [p for p in snapshot['products'] if p['id'] in excluded]
        barcodes = {p['barcode'] for p in blocked if p['barcode']}
        hashes = {e['sha256'] for p in blocked for e in p['examples']}
        perceptual = {e['dhash'] for p in blocked for e in p['examples'] if e.get('dhash')}
        with self.db.transaction() as connection:
            for product in snapshot['products']:
                similar = any(e['sha256'] in hashes or any(
                    (int(e.get('dhash', '0'), 16) ^ int(h, 16)).bit_count() <= 4 for h in perceptual)
                    for e in product['examples'])
                if product['id'] in excluded or product['barcode'] in barcodes or similar:
                    continue
                if any(example['capture_hash'] in target_hashes for example in product['examples']):
                    raise DatabaseError('Knowledge seed contains the test capture or its views')
                if set(product['fields']) - PRODUCT_FIELDS or product['verification'].get('status') != 'verified':
                    raise DatabaseError('Seed contains unverified or non-reusable product fields')
                validate_fields(product['fields'])
                now = utcnow()
                connection.execute('INSERT INTO product_profiles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (product['id'], self.cabinet_id, 'verified', product['version'], encode(product['fields']),
                     encode(product['aliases']), product['barcode'], encode(product['verification']), now, now))
                for claim in product.get('claims', []):
                    if claim['field'] not in PRODUCT_FIELDS or claim['value'] != product['fields'].get(claim['field']):
                        raise DatabaseError('Seed claim disagrees with product fields')
                    connection.execute('INSERT INTO product_claims VALUES (?, ?, ?, ?, ?)',
                        (new_id(), product['id'], claim['field'], encode(claim['value']), encode(claim['provenance'])))
                for example in product['examples']:
                    content = base64.b64decode(example['image_b64'], validate=True)
                    digest = hashlib.sha256(content).hexdigest()
                    if digest != example['sha256']:
                        raise Conflict('Seed image checksum mismatch')
                    if example.get('dhash') != image_hash(content):
                        raise Conflict('Seed perceptual checksum mismatch')
                    self.db.assets.mkdir(parents=True, exist_ok=True)
                    asset = self.db.assets / digest
                    asset.write_bytes(content)
                    connection.execute('INSERT INTO product_examples VALUES (?, ?, ?, ?, ?, ?)',
                        (new_id(), product['id'], str(asset.relative_to(self.db.path.parent)), digest,
                         example['capture_hash'], encode(example['metadata'])))
