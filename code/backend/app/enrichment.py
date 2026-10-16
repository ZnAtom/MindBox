"""Provenance-aware updates that cannot mutate physical inventory membership."""
import json

from .database import Conflict, DatabaseError
from .inventory_schemas import FieldSource, ItemValues
from .store import Store, item_value, require

IDENTITY_FIELDS = {'name', 'description', 'category', 'brand', 'variant', 'product_id', 'product_match_status'}
FACT_FIELDS = {'production_on', 'expires_on', 'shelf_life_raw', 'notes'}


class Enrichment:
    def __init__(self, store):
        self.store = store if isinstance(store, Store) else Store(store)

    def _validate_ref(self, connection, item, ref, source):
        if ref.startswith('speech:'):
            utterance = require(connection, 'speech_utterances', ref[7:])
            if source not in ('speech', 'derived') or utterance['item_id'] != item['id'] or utterance['link_status'] != 'linked':
                raise DatabaseError('Speech evidence is not linked to this physical item')
            return
        if ref.startswith('rag:'):
            identity, version = ref[4:].rsplit(':', 1)
            product = require(connection, 'product_profiles', identity)
            if (source != 'rag' or product['cabinet_id'] != item['cabinet_id'] or
                    product['status'] != 'verified' or str(product['version']) != version):
                raise DatabaseError('Product provenance is stale or outside the cabinet')
            return
        row = connection.execute('SELECT 1 FROM item_observations WHERE item_id = ? AND observation_id = ?',
                                 (item['id'], ref)).fetchone()
        if not row or source not in ('vision', 'ocr', 'barcode', 'derived'):
            raise DatabaseError('Visual evidence is not associated with this item')

    def apply(self, item_id, patch, provenance, expected_version, base_revision, idempotency_key,
              *, facts=False, cabinet_id='cabinet-01'):
        allowed = FACT_FIELDS if facts else IDENTITY_FIELDS
        if not patch or set(patch) - allowed or set(patch) != set(provenance):
            raise DatabaseError('Only whitelisted fields with per-field provenance can be enriched')
        provenance = {key: FieldSource.model_validate(value).model_dump() for key, value in provenance.items()}
        body = {'kind': 'item.facts' if facts else 'item.enrichment', 'item_id': item_id,
                'patch': patch, 'provenance': provenance, 'expected_version': expected_version,
                'base_revision': base_revision}
        store = self.store
        with store.db.transaction() as connection:
            receipt, duplicate = store._start_commit(connection, cabinet_id, idempotency_key, body,
                                                     base_revision, 'service:' + body['kind'])
            if duplicate:
                return receipt
            item = item_value(require(connection, 'items', item_id))
            if item['cabinet_id'] != cabinet_id or item['version'] != expected_version:
                raise Conflict('Item version or cabinet changed; revalidate the asynchronous result')
            values = {key: item[key] for key in ItemValues.model_fields}
            if patch.get('product_match_status') == 'verified' and not patch.get('product_id', item.get('product_id')):
                raise DatabaseError('Verified product identity requires a product profile')
            for key, meta in provenance.items():
                if item['field_meta'].get(key, {}).get('source') == 'user':
                    raise Conflict('Automatic enrichment cannot overwrite a user correction')
                if not meta['evidence_refs'] or meta['source'] in ('user', 'system', 'unknown', 'web'):
                    raise DatabaseError('Automatic updates require supported, traceable evidence')
                if facts and meta['source'] not in ('speech', 'ocr', 'barcode', 'derived'):
                    raise DatabaseError('Physical dates cannot be inherited from product memory')
                for ref in meta['evidence_refs']:
                    self._validate_ref(connection, item, ref, meta['source'])
            if patch.get('product_id'):
                product = require(connection, 'product_profiles', patch['product_id'])
                if product['cabinet_id'] != cabinet_id or product['status'] != 'verified':
                    raise DatabaseError('Only verified in-scope product profiles may be linked')
                reference = f"rag:{product['id']}:{product['version']}"
                if reference not in provenance['product_id']['evidence_refs']:
                    raise DatabaseError('Product link requires its exact verified version')
            values.update(patch)
            if values.get('product_id') and values['product_match_status'] != 'verified':
                raise DatabaseError('Product links and identity status must agree')
            values['field_meta'] = {**item['field_meta'], **provenance}
            before, after = store._write_item(connection, cabinet_id, values, item_id, expected_version)
            refs = sorted({r for meta in provenance.values() for r in meta['evidence_refs']})
            store._change(connection, receipt, 'update', before, after, refs, 0)
            if patch.get('product_id'):
                connection.execute('INSERT INTO item_product_links VALUES (?, ?, ?, ?) '
                    'ON CONFLICT(item_id) DO UPDATE SET product_id=excluded.product_id, '
                    'product_version=excluded.product_version, evidence_json=excluded.evidence_json',
                    (item_id, product['id'], product['version'], json.dumps(refs)))
            elif 'product_id' in patch:
                connection.execute('DELETE FROM item_product_links WHERE item_id=?', (item_id,))
            receipt['mutation_type'] = body['kind']
            return store._finish_commit(connection, receipt)
