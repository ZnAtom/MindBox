"""Inspect persisted results and actual tool traces; never invent database receipts."""
import json
from collections import Counter
from pathlib import Path

from .client import write_json


def inspect_run(path, output=None, database=None):
    path = Path(path)
    results = json.loads((path / 'summary.json').read_text(encoding='utf-8'))
    calls, reused_calls, per_video, modes = Counter(), Counter(), [], Counter()
    imported, receipts = [], []
    connection_context = None
    if database is not None:
        from .database import Database
        connection_context = Database(database)
    for result in results:
        trace_path = path / Path(result['video']).stem / 'tool-trace.json'
        trace = json.loads(trace_path.read_text(encoding='utf-8')) if trace_path.exists() else {}
        modes[trace.get('mode', 'not_recorded')] += 1
        video_calls = Counter()
        for entry in trace.get('trace', []):
            if result.get('recognition_reused'):
                reused_calls[entry['tool']] += 1
            else:
                video_calls[entry['tool']] += 1
                calls[entry['tool']] += 1
        action_trace_path = trace_path.with_name('action-tool-trace.json')
        if action_trace_path.exists():
            action_trace = json.loads(action_trace_path.read_text(encoding='utf-8'))
            for entry in action_trace.get('trace', []):
                video_calls[entry['tool']] += 1
                calls[entry['tool']] += 1
        video_receipts = []
        if connection_context is not None:
            with connection_context.connect() as connection:
                rows = connection.execute('SELECT r.id, r.result_json FROM processing_runs r '
                    'WHERE r.source_key = ?', (str((path / Path(result['video']).stem).resolve()),)).fetchall()
                for row in rows:
                    if json.loads(row['result_json']) != result:
                        continue
                    imported.append(row['id'])
                    video_receipts.extend(json.loads(r[0]) for r in connection.execute(
                        'SELECT receipt_json FROM inventory_commits WHERE run_id = ?', (row['id'],)))
                receipts.extend(video_receipts)
        per_video.append({'video': result['video'], 'pipeline_status': result['status'],
            'observation_candidates': len(result['items']),
            'objects': [{'candidate_id': item['candidate_id'], 'name': item['name'],
                         'category': item['category']} for item in result['items']],
            'events': [{k: e[k] for k in ('kind', 'candidate_ids', 'event_window_ms', 'committed')}
                       for e in result.get('event_candidates', [])],
            'tool_calls': dict(video_calls),
            'inventory_commit_receipts': video_receipts})
    summary = {'source_run': str(path), 'persistence': 'sqlite_and_archived_evidence' if imported else 'local_json_files',
        'database': {'status': 'connected' if database is not None else 'not_connected',
                     'imported_runs': len(imported), 'inventory_commits': len(receipts),
                     'inventory_revision': max((r['revision'] for r in receipts), default=None),
                     'note': 'Only matching imported runs and actual commit receipts are counted. Zero commits does not mean an empty cabinet.'},
        'video_count': len(results),
        'observation_candidates': sum(r['observation_candidates'] for r in per_video),
        'event_candidates': sum(len(r['events']) for r in per_video),
        'tool_modes': dict(modes), 'actual_tool_calls': dict(calls),
        'reused_recognition_tool_calls': dict(reused_calls),
        'per_video': per_video}
    if output:
        write_json(output, summary)
    print(json.dumps({k: summary[k] for k in ('persistence', 'database', 'video_count',
        'observation_candidates', 'event_candidates', 'actual_tool_calls')}, ensure_ascii=False), flush=True)
    return summary
