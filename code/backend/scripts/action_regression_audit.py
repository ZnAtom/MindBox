"""Read-only database/API audit of explicit action-regression directories."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import xml.etree.ElementTree as ET

from app.client import write_json
from app.config import ROOT
from app.experiments import file_hash


def database(path):
    with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return {'path': str(path), 'integrity': connection.execute('PRAGMA integrity_check').fetchone()[0],
                'items': connection.execute('SELECT count(*) FROM items').fetchone()[0],
                'commits': connection.execute('SELECT count(*) FROM inventory_commits').fetchone()[0],
                'revisions': [dict(r) for r in connection.execute('SELECT inventory_revision,inventory_known FROM cabinets')],
                'operations': [dict(r) for r in connection.execute('SELECT id,state FROM operations')]}


def audit(root):
    report = json.loads((root / 'report.json').read_text(encoding='utf-8'))
    evaluation = json.loads((root / 'evaluation.json').read_text(encoding='utf-8'))
    attempts = [json.loads(line) for path in root.glob('*/outputs/api-attempts.jsonl')
                for line in path.read_text(encoding='utf-8').splitlines()]
    cases = {}
    for name, case in report['cases'].items():
        context = root / name
        manifest = json.loads((context / 'manifest.json').read_text(encoding='utf-8'))
        cases[name] = {'analysis_pass': evaluation['cases'][name]['pass'],
            'intake_status': case['receipt']['status'], 'gate_reasons': case['gate']['reasons'],
            'evidence_route': case['gate'].get('evidence_route'),
            'items': [{k: item.get(k) for k in ('id', 'name', 'brand', 'variant', 'presence_status', 'version')}
                      for item in case['inventory']['items']],
            'unobserved_prefixes': case.get('unobserved_prefixes', []),
            'database': database(context / 'inventory.sqlite3'),
            'video_hashes_match': all(file_hash(v['path']) == v['sha256'] for v in manifest['videos']),
            'cache_seed': manifest.get('cache_seed', {}).get('source'),
            'report_sha256': file_hash(context / 'report.json')}
    return {'scope': report['scope'], 'mode': report['mode'], 'cases': cases,
            'api': {'statuses': dict(Counter(a['status'] for a in attempts)),
                    'http': dict(Counter(str(a['http_status']) for a in attempts if 'http_status' in a)),
                    'tokens_for_actual_requests': sum(a.get('usage', {}).get('total_tokens', 0) for a in attempts),
                    'failures': [a for a in attempts if a['status'] in ('failed', 'invalid_output')]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('roots', nargs='+', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    test = ET.parse(ROOT / 'var/review/p0-tests.xml').getroot()
    suites = [test] if test.tag == 'testsuite' else list(test.iter('testsuite'))
    result = {'tests': {k: sum(int(s.attrib.get(k, 0)) for s in suites)
                        for k in ('tests', 'failures', 'errors', 'skipped')},
              'experiments': {root.name: audit(root.resolve()) for root in args.roots},
              'production_database': database(ROOT / 'var/mind-box.sqlite3'),
              'limits': ['Frozen recognition and development videos, not independent accuracy.',
                         'Seeded exact-request caches are not new independent model observations.',
                         'No new ASR, year inference, reminder delivery or Docker validation in this regression.']}
    write_json(args.output, result)
    print(json.dumps({'tests': result['tests'], 'experiments': {
        name: {case: {'pass': value['analysis_pass'], 'intake': value['intake_status'],
                      'items': value['database']['items'], 'commits': value['database']['commits']}
               for case, value in experiment['cases'].items()}
        for name, experiment in result['experiments'].items()},
        'production_commits': result['production_database']['commits']}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
