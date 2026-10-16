"""Post-run regression evaluation; no model, video inference, or inventory writes."""
import json
from collections import Counter
from pathlib import Path

from .client import write_json


def evaluate_case(result, case):
    mapping = case['candidates']
    values = list(mapping.values())
    identity_valid = 'ambiguous' not in values and len(values) == len(set(values))
    history = [[event['kind'], mapping.get(cid, 'unmapped')]
               for event in result.get('event_candidates', []) for cid in event['candidate_ids']]
    expected = case['history']
    history_match = history == expected if case['ordered'] else Counter(map(tuple, history)) == Counter(map(tuple, expected))
    operation = result.get('operation_analysis', {})
    net_pairs = [(mapping.get(n['candidate_id'], 'unmapped'), n['change']) for n in operation.get('net_changes', [])]
    net_match = Counter(net_pairs) == Counter(case['net'].items())
    consistent = operation.get('status') == 'consistent'
    return {'video': result['video'], 'pass': bool(identity_valid and history_match and net_match and consistent and result['status'] == 'completed'),
            'history_matches': history_match, 'net_changes_match': net_match, 'evidence_consistent': consistent,
            'candidate_identity_unambiguous': identity_valid, 'history': history,
            'expected_history': expected, 'net': net_pairs, 'expected_net': case['net'],
            'pending': operation.get('pending', []), 'errors': result.get('errors', [])}


def evaluate_actions(run, annotations):
    run, annotations = Path(run), Path(annotations)
    labels = json.loads(annotations.read_text(encoding='utf-8'))
    results = json.loads((run / 'summary.json').read_text(encoding='utf-8'))
    reviewed = []
    for result in results:
        source = Path(result.get('recognition_source', ''))
        if source.parent.name != labels['frozen_recognition_run']:
            raise ValueError('Candidate annotations do not match the frozen recognition run; re-annotate crop IDs')
        reviewed.append(evaluate_case(result, labels['cases'][Path(result['video']).stem]))
    report = {'scope': 'Development-set action regression with frozen recognition; not blind accuracy or database commits.',
              'cases': reviewed, 'passed_videos': [r['video'] for r in reviewed if r['pass']],
              'failed_or_partial_videos': [r['video'] for r in reviewed if not r['pass']]}
    write_json(run / 'action-evaluation.json', report)
    print(json.dumps({k: report[k] for k in ('passed_videos', 'failed_or_partial_videos')}, ensure_ascii=False), flush=True)
    return report
