import json

from app.review import inspect_run


def test_json_candidates_are_not_reported_as_database_commits(tmp_path):
    summary = [{'video': 'v1.mp4', 'status': 'completed', 'items': [],
                'event_candidates': [{'kind': 'remove', 'candidate_ids': ['a'],
                    'event_window_ms': [0, 1000], 'committed': False}]}]
    (tmp_path / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')
    trace_dir = tmp_path / 'v1'
    trace_dir.mkdir()
    (trace_dir / 'tool-trace.json').write_text(json.dumps({'mode': 'native_function_call',
        'trace': [{'tool': 'search_catalog', 'result': {'status': 'ok'}}]}), encoding='utf-8')
    result = inspect_run(tmp_path)
    assert result['database']['status'] == 'not_connected'
    assert result['database']['inventory_commits'] == 0
    assert result['actual_tool_calls'] == {'search_catalog': 1}
    assert result['event_candidates'] == 1
    assert result['per_video'][0]['inventory_commit_receipts'] == []


def test_reused_recognition_tools_are_not_counted_as_new_calls(tmp_path):
    summary = [{'video': 'v1.mp4', 'status': 'completed', 'items': [], 'recognition_reused': True}]
    (tmp_path / 'summary.json').write_text(json.dumps(summary), encoding='utf-8')
    folder = tmp_path / 'v1'
    folder.mkdir()
    (folder / 'tool-trace.json').write_text(json.dumps({'trace': [{'tool': 'search_catalog'}]}), encoding='utf-8')
    (folder / 'action-tool-trace.json').write_text(json.dumps({'trace': [{'tool': 'get_frames'}]}), encoding='utf-8')
    result = inspect_run(tmp_path)
    assert result['actual_tool_calls'] == {'get_frames': 1}
    assert result['reused_recognition_tool_calls'] == {'search_catalog': 1}
