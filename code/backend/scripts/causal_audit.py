"""Read-only audit of causal experiments; no model calls or reference answers."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3

from app.client import write_json


def inspect(root):
    root = root.resolve()
    session = json.loads((root / 'causal/session.json').read_text(encoding='utf-8'))
    path = root / 'api/api-attempts.jsonl'
    attempts = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []
    uri = (root / 'inventory.sqlite3').as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
        foreign_keys = list(connection.execute('PRAGMA foreign_key_check'))
        items = [dict(r) for r in connection.execute('SELECT id,name,category,presence_status,version FROM items')]
        receipts = [json.loads(r[0]) for r in connection.execute('SELECT receipt_json FROM inventory_commits ORDER BY revision')]
        events = [json.loads(r[0]) for r in connection.execute('SELECT payload_json FROM verified_events')]
        revision = connection.execute('SELECT inventory_revision FROM cabinets').fetchone()[0]
    image_counts = [json.loads(p.read_text(encoding='utf-8'))['image_count']
                    for p in (root / 'causal').glob('prompt-images-*.json')]
    return {'experiment': root.name, 'status': session['status'], 'steps': len(session['steps']),
        'pending': {tid: t['pending'] for tid, t in session['ledger']['tracks'].items() if t['pending']},
        'step_failures': [{k: s[k] for k in ('index', 'error', 'verification_failures') if k in s}
                          for s in session['steps'] if s['status'] == 'pending'],
        'http': dict(Counter(str(e.get('http_status')) for e in attempts if 'http_status' in e)),
        'api_status': dict(Counter(e['status'] for e in attempts)),
        'tokens': sum(e.get('usage', {}).get('total_tokens', 0) for e in attempts if e['status'] == 'success'),
        'failed_attempts': [{k: e[k] for k in ('stage', 'attempt', 'http_status', 'error') if k in e}
                            for e in attempts if e['status'] == 'failed'],
        'maximum_observation_images': max(image_counts, default=None),
        'inventory_revision': revision, 'items': items, 'receipts': receipts,
        'verified_events': events, 'results': session.get('results', []),
        'integrity': integrity, 'foreign_key_errors': len(foreign_keys),
        'ledger_matches_db_revision': (session['inventory']['inventory_revision'] == revision
                                       if 'inventory' in session else None)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('roots', type=Path, nargs='+')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = [inspect(root) for root in args.roots]
    write_json(args.output, report)
    for row in report:
        print(json.dumps({k: row[k] for k in ('experiment', 'status', 'inventory_revision',
            'http', 'tokens', 'maximum_observation_images', 'pending', 'integrity')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
