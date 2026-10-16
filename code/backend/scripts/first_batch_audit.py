"""Read-only consolidation of real first-batch evidence and isolated DB receipts."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.client import write_json
from app.experiments import Experiment, file_hash
from app.tasks import Tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--experiments', type=Path, default=Path('var/experiments'))
    parser.add_argument('--assisted', default='batch-a-assisted-v10-r2')
    parser.add_argument('--asr', type=Path, default=Path('var/runs/batch-a-asr'))
    parser.add_argument('--tests', type=Path, default=Path('var/review/batch-a-tests.xml'))
    parser.add_argument('--output', type=Path, default=Path('var/review/batch-a-audit.json'))
    args = parser.parse_args()
    inventory = {}
    roots = [args.experiments / name for name in ('batch-a-v9-cold', 'batch-a-v10-cold', args.assisted)]
    for root in roots:
        context = Experiment.open(root)
        inventory[root.name] = {'inventory': context.store.get_inventory_snapshot(),
            'tasks': Tasks(context).list(), 'receipts': context.store.receipts('cabinet-01'),
            'database': context.store.db.status(), 'manifest_sha256': file_hash(root / 'manifest.json')}
    attempts = []
    for path in args.experiments.rglob('api-attempts.jsonl'):
        if any(part.startswith('batch-a-') for part in path.parts):
            attempts.extend(json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip())
    asr = {p.name: json.loads(p.read_text(encoding='utf-8')) for p in args.asr.glob('*.json')}
    tests = ET.parse(args.tests).getroot()
    suites = list(tests) if tests.tag == 'testsuites' else [tests]
    stats = {key: sum(int(s.attrib.get(key, 0)) for s in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
    production = Path('var/mind-box.sqlite3')
    with sqlite3.connect(production.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        production_status = {'migrations': [r[0] for r in connection.execute('SELECT version FROM schema_migrations ORDER BY version')],
            'inventory_revisions': list(connection.execute('SELECT id, inventory_revision FROM cabinets')),
            'commits': connection.execute('SELECT count(*) FROM inventory_commits').fetchone()[0]}
    hashes = {str(p): file_hash(p) for folder in ('app', 'tests', 'scripts', 'migrations')
              for p in Path(folder).rglob('*') if p.suffix in ('.py', '.sql', '.txt')}
    report = {'tests': stats, 'experiments': inventory, 'production_read_only_check': production_status,
        'api': {'statuses': dict(Counter(a['status'] for a in attempts)),
            'http_statuses': dict(Counter(str(a.get('http_status')) for a in attempts if a.get('http_status'))),
            'total_tokens': sum(a.get('usage', {}).get('total_tokens', 0) for a in attempts if a['status'] == 'success'),
            'failures': [a for a in attempts if a['status'] in ('failed', 'invalid_output')]},
        'asr': asr, 'verified_source_hashes': hashes,
        'scope': 'Development samples and assisted evidence review; no independent accuracy claim'}
    write_json(args.output, report)
    print(json.dumps({'tests': stats, 'api': {k: v for k, v in report['api'].items() if k != 'failures'},
        'asr_statuses': {k: v['status'] for k, v in asr.items()},
        'production': production_status, 'report': str(args.output)}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
