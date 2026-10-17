"""Prevent misleading scores from duplicate IDs, labels, or unlabelled moves."""
from model_comparison.evaluate import score_case


def dense_result():
    return {'video': 'test.mp4', 'status': 'completed', 'objects': [{'object_id': 'new-1', 'identity_uncertain': False}],
            'events': [{'object_id': 'new-1', 'kind': 'add'}],
            'net_changes': [{'object_id': 'new-1', 'change': 'add'}]}


CASE = {'ordered': False, 'history': [['add', 'drink']], 'net': {'drink': 'add'}}
REVIEW = {'mapping': {'new-1': 'drink'}, 'basic_label_errors': [], 'result_sha256': 'test'}


def test_deepseek_dense_uses_the_same_scoring_as_gpt_dense():
    assert score_case(dense_result(), CASE, REVIEW, 'deepseek_dense') == score_case(dense_result(), CASE, REVIEW, 'gpt_dense')


def test_wrong_name_does_not_erase_correct_motion_but_cannot_pass_whole_case():
    result = score_case(dense_result(), CASE, {**REVIEW, 'basic_label_errors': ['called honey']}, 'gpt_dense')
    assert result['events_exact'] and result['net_exact'] and result['instances_exact']
    assert not result['recognition_exact'] and not result['complete_correct']


def test_duplicate_physical_instance_is_not_deduplicated_into_a_pass():
    result = dense_result()
    result['objects'].append({'object_id': 'new-2', 'identity_uncertain': False})
    result['net_changes'].append({'object_id': 'new-2', 'change': 'add'})
    score = score_case(result, CASE, {**REVIEW, 'mapping': {'new-1': 'drink', 'new-2': 'drink'}}, 'gpt_dense')
    assert not score['instances_exact'] and not score['net_exact'] and not score['complete_correct']


def test_auxiliary_moves_excluded_equally_from_storage_action_score():
    result = dense_result()
    result['events'].append({'object_id': 'new-1', 'kind': 'move'})
    score = score_case(result, CASE, REVIEW, 'gpt_dense')
    assert score['complete_correct'] and score['auxiliary_moves_not_scored'] == [('move', 'drink')]
