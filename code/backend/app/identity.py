"""Apply reviewed crop identity to an already committed physical item."""
import json

from .database import DatabaseError
from .enrichment import Enrichment
from .store import require


def enrich_candidate(context, run_id, candidate_id, item_id):
    with context.store.db.connect() as connection:
        run = require(connection, 'processing_runs', run_id)
        result = json.loads(run['result_json'])
        candidate = next((i for i in result['items'] if i['candidate_id'] == candidate_id), None)
        if not candidate or candidate.get('localization_status') != 'usable' or candidate.get('target_kind') != 'movable_item':
            raise DatabaseError('Identity enrichment requires a usable, reviewed object crop')
        refs = [r['id'] for r in connection.execute(
            'SELECT o.id FROM observations o JOIN item_observations io ON io.observation_id=o.id '
            "WHERE o.run_id=? AND io.item_id=? AND o.kind='crop'", (run_id, item_id))]
        if not refs:
            raise DatabaseError('Candidate crop is not associated with this committed item')
        known_refs = {r['id']: r['local_ref'] for r in connection.execute('SELECT id, local_ref FROM observations WHERE run_id=?', (run_id,))}
        refs = [ref for ref in refs if known_refs[ref] in candidate['evidence_refs']]
        if not refs:
            raise DatabaseError('Candidate and physical item evidence do not match')
        key = f'identity:{run_id}:{candidate_id}:{item_id}'
        prior = connection.execute('SELECT receipt_json FROM inventory_commits WHERE idempotency_key=? AND cabinet_id=?',
                                   (key, 'cabinet-01')).fetchone()
        if prior:
            return json.loads(prior[0])
    item = context.store.get_item(item_id)
    patch = {f: candidate[f] for f in ('name', 'name_kind', 'category', 'description', 'brand', 'variant') if candidate.get(f)}
    patch['product_match_status'] = 'candidate'
    provenance = {f: {'source': 'vision', 'certainty': 'estimated', 'evidence_refs': refs,
                      'extractor': 'verified-crops:' + run_id} for f in patch}
    return Enrichment(context.store).apply(item_id, patch, provenance, item['version'],
        context.store.get_inventory_snapshot()['inventory_revision'], key)
