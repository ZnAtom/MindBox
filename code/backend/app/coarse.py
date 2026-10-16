"""Conservative, development-only gate for one unambiguous offline insertion."""
import json

from .actions import presence, selected_observation
from .database import Conflict, DatabaseError, encode, new_id
from .store import require


class CoarseIntake:
    def __init__(self, context):
        self.context, self.store = context, context.store

    def assess(self, run_id):
        with self.store.db.connect() as connection:
            run = require(connection, 'processing_runs', run_id)
            operation = require(connection, 'operations', run['operation_id'])
        result, artifacts = json.loads(run['result_json']), json.loads(run['artifacts_json'])
        analysis = result.get('operation_analysis', {})
        reasons = []
        if run['status'] != 'completed' or analysis.get('status') != 'consistent' or analysis.get('pending'):
            reasons.append('analysis_pending_or_incomplete')
        if result.get('possible_duplicate_links'):
            reasons.append('possible_duplicate_identity')
        net = analysis.get('net_changes', [])
        additions = [n['candidate_id'] for n in net if n['change'] == 'add']
        if len(additions) != 1 or any(n['change'] not in ('add', 'kept', 'not_present') for n in net):
            reasons.append('only_single_insertion_supported_in_batch_a')
        if len(additions) != 1:
            return {'status': 'pending', 'reasons': reasons, 'run_id': run_id,
                    'operation_id': operation['id'], 'base_revision': run['base_revision'],
                    'policy': 'single-insertion-evidence-v2', 'evidence_route': None}
        cid = additions[0]
        item = next(i for i in result['items'] if i['candidate_id'] == cid)
        if item.get('localization_status') != 'usable' or item.get('target_kind') != 'movable_item':
            reasons.append('object_crop_not_verified_usable')
        snapshots = artifacts.get('snapshot-states.json', {}).get('objects', [])
        snapshot = next((o for o in snapshots if o['candidate_id'] == cid), None)
        baseline = analysis.get('baseline_observations', {}).get(cid)
        endpoints = {**snapshot, 'before': baseline['state']} if snapshot and baseline else snapshot
        if baseline and (baseline['identity'] != 'supported' or baseline['support'] not in ('high', 'medium')):
            reasons.append('initial_identity_not_supported')
        if not endpoints or not self._placement(endpoints):
            reasons.append('clear_before_and_after_snapshots_required')
        events = analysis.get('event_candidates', [])
        if len(events) != 1 or events[0].get('kind') != 'add' or events[0]['candidate_ids'] != [cid]:
            reasons.append('mixed_or_unresolved_event_history')
        windows = artifacts.get('window-states.json', [])
        supported_crossings, stable_confirmations = [], []
        for window in windows:
            observation, conflicts = selected_observation(window)
            if any(c['candidate_id'] == cid for c in conflicts):
                reasons.append('review_reversed_prior_observation')
            for obj in observation['objects']:
                if obj['candidate_id'] != cid:
                    continue
                if (not observation['drawer_only'] and self._placement(obj)
                        and obj['evidence'] == 'crossing' and obj['drawer_relative_motion']):
                    supported_crossings.append(obj)
                if (not observation['drawer_only'] and obj['identity'] == 'supported'
                        and obj['support'] in ('high', 'medium')
                        and obj['evidence'] in ('crossing', 'comparable_states')
                        and obj['after']['location'] == 'inside' and obj['after']['at_rest']
                        and obj['after']['interior_clear']):
                    stable_confirmations.append(obj)
        route = 'direct_crossing' if supported_crossings else 'stable_state_comparison' if stable_confirmations else None
        if route is None:
            reasons.append('no_supported_placement_confirmation')
        if any(r.get('candidate_id') == cid for r in artifacts.get('rejected-state-claims.json', [])):
            reasons.append('rejected_visibility_claims')
        with self.store.db.connect() as connection:
            times = {r['local_ref']: r['media_time_ms'] for r in connection.execute(
                'SELECT * FROM observations WHERE run_id=?', (run_id,))}
        if endpoints:
            for obj in [endpoints] + supported_crossings:
                before, after = obj['before']['frame_ids'], obj['after']['frame_ids']
                if not before or not after or not set(before + after) <= times.keys():
                    reasons.append('missing_timestamped_evidence')
                elif max(times[f] for f in before) >= min(times[f] for f in after):
                    reasons.append('unordered_placement_evidence')
        return {'status': 'pending' if reasons else 'eligible', 'reasons': sorted(set(reasons)),
                'candidate_id': cid, 'operation_id': operation['id'], 'run_id': run_id,
                'base_revision': run['base_revision'], 'policy': 'single-insertion-evidence-v2',
                'evidence_route': route,
                'endpoint_source': baseline['source'] if baseline else 'snapshot'}

    @staticmethod
    def _placement(obj):
        return (obj['identity'] == 'supported' and obj['support'] in ('high', 'medium')
                and presence(obj['before']) is False
                and obj['after']['location'] == 'inside' and obj['after']['at_rest']
                and obj['after']['interior_clear'])

    def commit(self, run_id):
        with self.store.db.connect() as connection:
            existing = connection.execute('SELECT receipt_json FROM inventory_commits WHERE run_id=? AND idempotency_key=?',
                                          (run_id, 'coarse:' + run_id)).fetchone()
        if existing:
            return json.loads(existing['receipt_json'])
        assessment = self.assess(run_id)
        if assessment['status'] != 'eligible':
            with self.store.db.transaction() as connection:
                run = require(connection, 'processing_runs', run_id)
                payload = encode({'reason': 'coarse_gate_rejected', 'assessment': assessment})
                existing = connection.execute('SELECT 1 FROM pending_candidates WHERE run_id=? AND payload_json=?',
                                              (run_id, payload)).fetchone()
                if not existing:
                    connection.execute('INSERT INTO pending_candidates VALUES (?, ?, ?, NULL, NULL, NULL)',
                                       (new_id(), run_id, payload))
                connection.execute("UPDATE operations SET state='pending' WHERE id=? AND state IN ('ready','pending')",
                                   (run['operation_id'],))
            return assessment
        with self.store.db.connect() as connection:
            run = require(connection, 'processing_runs', run_id)
            result = json.loads(run['result_json'])
            item = next(i for i in result['items'] if i['candidate_id'] == assessment['candidate_id'])
        plan = {k: assessment[k] for k in ('run_id', 'operation_id', 'base_revision')}
        plan.update(idempotency_key='coarse:' + run_id, reviewed_by='policy:' + assessment['policy'],
                    coarse_only=True, decisions=[{'candidate_id': item['candidate_id'], 'action': 'add',
                                                 'evidence_refs': item['evidence_refs']}])
        try:
            return self.store.commit_inventory(plan)
        except (Conflict, DatabaseError) as exc:
            return {'status': 'pending', 'reasons': [str(exc)], 'assessment': assessment}
