import pytest

from app.action_schemas import ScenePlan, StateComparison
from app.actions import EvidenceTools, guard_states, reconcile, validate_plan, validate_states


def state(location, frame):
    return {'location': location, 'frame_ids': [frame], 'interior_clear': location != 'occluded',
            'at_rest': location == 'inside', 'description': location}


def transition(cid, before, after, a, b):
    return {'candidate_id': cid, 'before': state(before, a), 'after': state(after, b),
            'identity': 'supported', 'evidence': 'comparable_states',
            'drawer_relative_motion': True, 'support': 'medium'}


def observation(objects, drawer_only=False):
    return {'objects': objects, 'drawer_only': drawer_only, 'uncertainties': []}


def test_return_preserves_four_step_history_and_net_inventory():
    frames = {str(i): {'media_time_ms': i * 100} for i in range(10)}
    snapshots = observation([transition('a', 'inside', 'inside', '0', '9'),
                             transition('b', 'inside', 'absent', '0', '9'),
                             transition('c', 'absent', 'inside', '0', '9')])
    windows = [{'window_id': str(i), 'observation': observation([t])} for i, t in enumerate([
        transition('a', 'inside', 'outside', '0', '1'),
        transition('c', 'outside', 'inside', '2', '3'),
        transition('b', 'inside', 'outside', '4', '5'),
        transition('a', 'outside', 'inside', '6', '7')])]
    result = reconcile(snapshots, windows, frames)
    assert result['status'] == 'consistent'
    assert [e['kind'] for e in result['event_candidates']] == ['remove', 'add', 'remove', 'return']
    assert {n['candidate_id']: n['change'] for n in result['net_changes']} == {'a': 'kept', 'b': 'remove', 'c': 'add'}
    assert not any(e['committed'] for e in result['event_candidates'])


def test_closed_drawer_occlusion_cannot_remove_item():
    frames = {'a': {'media_time_ms': 0}, 'b': {'media_time_ms': 1000}}
    snapshots = observation([transition('x', 'inside', 'inside', 'a', 'b')])
    closed = transition('x', 'inside', 'occluded', 'a', 'b')
    closed['evidence'] = 'occlusion_only'
    result = reconcile(snapshots, [{'window_id': 'closing', 'observation': observation([closed], True)}], frames)
    assert result['event_candidates'] == []
    assert result['net_changes'][0]['change'] == 'kept'
    assert result['pending']


def test_missing_event_is_detected_by_independent_snapshots():
    snapshots = observation([transition('x', 'absent', 'inside', 'a', 'b')])
    result = reconcile(snapshots, [], {'a': {'media_time_ms': 0}, 'b': {'media_time_ms': 1}})
    assert result['status'] == 'pending'
    assert result['pending'][0]['reason'] == 'history_does_not_match_final_snapshot'


def test_return_can_bridge_occlusion_from_a_verified_prior_removal():
    frames = {str(i): {'media_time_ms': i * 100} for i in range(5)}
    snapshots = observation([transition('a', 'inside', 'inside', '0', '4')])
    returning = transition('a', 'unknown', 'inside', '2', '4')
    windows = [{'window_id': 'out', 'observation': observation([transition('a', 'inside', 'outside', '0', '1')])},
               {'window_id': 'back', 'observation': observation([returning])}]
    result = reconcile(snapshots, windows, frames)
    assert [e['kind'] for e in result['event_candidates']] == ['remove', 'return']
    assert result['event_candidates'][1]['before_frame_id'] == '1'
    assert result['event_candidates'][1]['bridged_from_prior_evidence']
    assert result['status'] == 'consistent'


def test_unchanged_observation_cannot_silently_contradict_removal():
    frames = {str(i): {'media_time_ms': i * 100} for i in range(5)}
    snapshots = observation([transition('a', 'inside', 'absent', '0', '4')])
    windows = [{'window_id': 'out', 'observation': observation([transition('a', 'inside', 'outside', '0', '1')])},
               {'window_id': 'wrong', 'observation': observation([transition('a', 'inside', 'inside', '2', '3')])}]
    result = reconcile(snapshots, windows, frames)
    assert result['status'] == 'pending'
    assert result['pending'][0]['reason'] == 'unchanged_observation_conflicts_with_history'


def test_drawer_motion_cannot_override_a_state_conflict():
    t = transition('x', 'inside', 'absent', 'a', 'b')
    result = reconcile(observation([t]), [{'window_id': 'closing', 'observation': observation([t], True)}],
                       {'a': {'media_time_ms': 0}, 'b': {'media_time_ms': 1}})
    assert not result['event_candidates']
    assert result['status'] == 'pending'


def test_absence_and_frame_references_require_actual_evidence():
    t = transition('x', 'absent', 'inside', 'a', 'b')
    t['before']['interior_clear'] = False
    with pytest.raises(ValueError, match='Absence'):
        validate_states(StateComparison.model_validate(observation([t])), {'x'}, {'a'}, {'b'},
                        {'a': {'media_time_ms': 0}, 'b': {'media_time_ms': 1}})
    t['before']['interior_clear'] = True
    with pytest.raises(ValueError, match='outside'):
        validate_states(StateComparison.model_validate(observation([t])), {'x'}, {'a'}, {'a'}, {})


def test_unseen_absence_is_downgraded_without_discarding_other_objects():
    t = transition('x', 'inside', 'absent', 'a', 'b')
    t['after']['interior_clear'] = False
    rejected = []
    value = guard_states(StateComparison.model_validate(observation([t, transition('y', 'outside', 'inside', 'a', 'b')])), rejected)
    assert value.objects[0].after.location == 'unknown'
    assert value.objects[1].after.location == 'inside'
    assert rejected[0]['original']['location'] == 'absent'
    assert rejected[0]['original']['interior_clear'] is False


def test_snapshot_cannot_be_chosen_after_drawer_closes():
    plan = ScenePlan.model_validate({'phases': [{'phase': 'closed', 'start_frame_id': 'a',
        'end_frame_id': 'b', 'interior_visible': False}], 'before_snapshot': None,
        'after_snapshot': {'frame_id': 'b', 'bbox': [0, 0, 1000, 1000]}, 'windows': [], 'uncertainties': []})
    with pytest.raises(ValueError, match='open'):
        validate_plan(plan, {'a': {'media_time_ms': 0}, 'b': {'media_time_ms': 1000}}, {'a', 'b'})


def test_evidence_tools_reject_unknown_paths_and_inventory_is_not_empty(tmp_path):
    toolbox = EvidenceTools(tmp_path / 'video.mp4', tmp_path, tmp_path, {'items': []},
                            [{'id': 'f', 'media_time_ms': 1000}])
    result, _ = toolbox.call('get_inventory_snapshot', {})
    assert result['status'] == 'unavailable' and result['inventory'] is None
    result, _ = toolbox.call('get_frames', {'start_ms': 0, 'end_ms': 2000, 'fps': 12})
    assert result['status'] == 'rejected'
    result, _ = toolbox.call('get_object_evidence', {'candidate_id': '../secret'})
    assert result['status'] == 'rejected'
