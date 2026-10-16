from app.evaluation import evaluate_case


def test_consistent_wrong_action_is_not_a_pass():
    result = {'video': 'v.mp4', 'status': 'completed', 'event_candidates': [{'kind': 'remove', 'candidate_ids': ['x']}],
        'operation_analysis': {'status': 'consistent', 'net_changes': [{'candidate_id': 'x', 'change': 'remove'}]}}
    case = {'candidates': {'x': 'drink'}, 'ordered': True, 'history': [['add', 'drink']], 'net': {'drink': 'add'}}
    assert not evaluate_case(result, case)['pass']


def test_four_correct_actions_with_conflicting_evidence_still_do_not_pass():
    result = {'video': 'v.mp4', 'status': 'completed', 'event_candidates': [{'kind': 'return', 'candidate_ids': ['x']}],
        'operation_analysis': {'status': 'pending', 'net_changes': [{'candidate_id': 'x', 'change': 'kept'}]}}
    case = {'candidates': {'x': 'snack'}, 'ordered': True, 'history': [['return', 'snack']], 'net': {'snack': 'kept'}}
    reviewed = evaluate_case(result, case)
    assert reviewed['history_matches'] and reviewed['net_changes_match'] and not reviewed['pass']
