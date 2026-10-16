"""Validate real frozen evidence against an isolated database without model calls."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import Database
from app.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.database.exists():
        raise ValueError('Smoke validation requires a new, isolated database path')
    source = args.source.resolve()
    result = json.loads((source / 'result.json').read_text(encoding='utf-8'))
    net = result['operation_analysis']['net_changes']
    if not net or any(entry['change'] != 'add' for entry in net):
        raise ValueError('This smoke case requires an all-add result; it never guesses existing identities')
    store = Store(Database(args.database))
    store.initialize('validation-only', known_empty=True)
    imported = store.import_run(source, 'validation-only', base_revision=0)
    candidates = {c['candidate_id']: c for c in result['items']}
    plan = {'operation_id': imported['operation_id'], 'run_id': imported['run_id'], 'base_revision': 0,
            'idempotency_key': 'database-validation', 'reviewed_by': 'isolated-database-test',
            'decisions': [{'candidate_id': n['candidate_id'], 'action': 'add',
                           'evidence_refs': candidates[n['candidate_id']]['evidence_refs']} for n in net]}
    receipt = store.commit_inventory(plan)
    assert store.commit_inventory(plan) == receipt
    assert store.import_run(source, 'validation-only', base_revision=0)['duplicate']
    backup = store.db.backup(args.database.parent / (args.database.stem + '-backup'))
    restored = Store(backup['database'])
    assert restored.get_inventory_snapshot('validation-only') == store.get_inventory_snapshot('validation-only')
    snapshot = store.get_inventory_snapshot('validation-only')
    report = {'mode': 'isolated_database_validation', 'source': str(source), 'database': str(store.db.path),
              'note': 'Known-empty baseline and reviewer are test setup, not a verified physical cabinet inventory.',
              'import': imported, 'receipt': receipt, 'snapshot': snapshot,
              'retry_returns_same_receipt': True, 'duplicate_import_reused': True,
              'backup_restored': True, 'integrity': store.db.status()['integrity'], 'backup': backup}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(args.output), 'observations': imported['observation_count'],
                      'inventory_items': len(snapshot['items']), 'revision': snapshot['inventory_revision'],
                      'backup_restored': True, 'integrity': report['integrity']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
