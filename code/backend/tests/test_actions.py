import pytest

from app.action_schemas import ScenePlan, StateComparison
from app.actions import EvidenceTools, guard_states, instance_conflicts, reconcile, review_conflicts, validate_plan, validate_states


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


def test_placement_finishes_in_next_window_once():
    frames = {str(i): {'media_time_ms': i * 100} for i in range(5)}
    held = transition('a', 'outside', 'inside', '0', '1')
    held['after']['at_rest'] = False
    stable = transition('a', 'inside', 'inside', '2', '3')
    result = reconcile(observation([transition('a', 'outside', 'inside', '0', '4')]), [
        {'window_id': 'enter', 'observation': observation([held])},
        {'window_id': 'release', 'observation': observation([stable])}], frames)
    assert result['status'] == 'consistent'
    assert [e['kind'] for e in result['event_candidates']] == ['add']
    event = result['event_candidates'][0]
    assert event['before_frame_id'] == '0' and event['after_frame_id'] == '3'
    assert event['placement_completed_in_later_window']
    assert event['evidence_window_ids'] == ['enter', 'release']


def test_held_object_is_not_added_without_release():
    frames = {'a': {'media_time_ms': 0}, 'b': {'media_time_ms': 100}}
    held = transition('a', 'outside', 'inside', 'a', 'b')
    held['after']['at_rest'] = False
    result = reconcile(observation([held]), [{'window_id': 'held', 'observation': observation([held])}], frames)
    assert result['event_candidates'] == []
    assert any(p['reason'] == 'placement_not_completed' for p in result['pending'])


def test_held_inside_window_can_continue_from_an_earlier_outside_baseline():
    frames = {str(i): {'media_time_ms': i * 100} for i in range(5)}
    held = transition('a', 'inside', 'inside', '1', '2')
    held['before']['at_rest'] = held['after']['at_rest'] = False
    stable = transition('a', 'inside', 'inside', '3', '4')
    result = reconcile(observation([transition('a', 'outside', 'inside', '0', '4')]), [
        {'window_id': 'held', 'observation': observation([held])},
        {'window_id': 'stable', 'observation': observation([stable])}], frames)
    assert result['status'] == 'consistent'
    assert [e['kind'] for e in result['event_candidates']] == ['add']
    assert result['event_candidates'][0]['before_frame_id'] == '0'
    assert result['placement_completions'][0]['completed_window_id'] == 'stable'


def test_same_frame_conflict_cannot_start_a_bridged_placement():
    frames = {str(i): {'media_time_ms': i * 100} for i in range(3)}
    held = transition('a', 'inside', 'inside', '0', '1')
    held['before']['at_rest'] = held['after']['at_rest'] = False
    result = reconcile(observation([transition('a', 'outside', 'inside', '0', '2')]), [
        {'window_id': 'held', 'observation': observation([held])},
        {'window_id': 'stable', 'observation': observation([transition('a', 'inside', 'inside', '1', '2')])}], frames)
    assert result['status'] == 'pending' and not result['event_candidates']


def test_held_object_leaving_does_not_count_as_stored_then_removed():
    frames = {str(i): {'media_time_ms': i * 100} for i in range(4)}
    held = transition('a', 'outside', 'inside', '0', '1')
    held['after']['at_rest'] = False
    leaving = transition('a', 'inside', 'outside', '1', '2')
    leaving['before']['at_rest'] = False
    result = reconcile(observation([transition('a', 'outside', 'outside', '0', '3')]), [
        {'window_id': 'held', 'observation': observation([held])},
        {'window_id': 'leave', 'observation': observation([leaving])}], frames)
    assert result['status'] == 'consistent' and result['event_candidates'] == []
    assert result['cancelled_placements']


def test_later_snapshot_is_checkpoint_instead_of_earlier_inventory():
    frames = {str(i): {'media_time_ms': i * 100} for i in range(5)}
    result = reconcile(observation([transition('a', 'inside', 'inside', '2', '4')]), [
        {'window_id': 'early', 'observation': observation([transition('a', 'absent', 'inside', '0', '1')])}], frames)
    assert result['status'] == 'consistent'
    assert result['net_changes'][0]['change'] == 'add'
    assert result['baseline_observations']['a']['media_time_ms'] == 0
    assert result['snapshot_checkpoints'][0]['status'] == 'matched'


def test_future_absence_cannot_bridge_an_earlier_unknown_state():
    frames = {str(i): {'media_time_ms': i * 100} for i in range(5)}
    result = reconcile(observation([transition('a', 'absent', 'inside', '3', '4')]), [
        {'window_id': 'early', 'observation': observation([transition('a', 'unknown', 'inside', '0', '1')])}], frames)
    assert result['status'] == 'pending' and result['event_candidates'] == []
    assert any(p['reason'] == 'occlusion_or_unknown_state' for p in result['pending'])


def test_first_visible_existing_item_is_a_dated_baseline_not_an_invented_add():
    frames = {str(i): {'media_time_ms': i * 100} for i in range(5)}
    result = reconcile(observation([transition('a', 'inside', 'inside', '3', '4')]), [
        {'window_id': 'opening', 'observation': observation([transition('a', 'unknown', 'inside', '0', '1')])}], frames)
    assert result['status'] == 'consistent' and result['event_candidates'] == []
    assert result['net_changes'][0]['change'] == 'kept'
    assert result['baseline_observations']['a']['media_time_ms'] == 100
    assert result['unobserved_prefixes'][0]['until_ms'] == 100


def test_prior_outside_evidence_is_not_rewritten_as_clear_interior_absence():
    frames = {str(i): {'media_time_ms': i * 100} for i in range(4)}
    snapshots = observation([transition('a', 'outside', 'inside', '0', '3')])
    snapshots['objects'][0]['before']['interior_clear'] = False
    result = reconcile(snapshots, [{'window_id': 'entry', 'observation': observation([
        transition('a', 'unknown', 'inside', '1', '2')])}], frames)
    assert result['status'] == 'consistent'
    evidence = result['event_candidates'][0]['state_evidence']['before']
    assert evidence['location'] == 'outside' and evidence['interior_clear'] is False


def test_reversed_review_keeps_original_and_requires_arbitration():
    frames = {str(i): {'media_time_ms': i * 100} for i in range(3)}
    original = observation([transition('a', 'outside', 'absent', '0', '1')])
    revised = observation([transition('a', 'inside', 'outside', '0', '1')])
    result = reconcile(observation([transition('a', 'outside', 'absent', '0', '2')]), [
        {'window_id': 'open', 'initial_observation': original, 'observation': revised}], frames)
    assert result['event_candidates'] == []
    assert any(p['reason'] == 'review_reversed_known_presence' for p in result['pending'])


def test_same_frame_rest_reversal_is_not_accepted_but_unknown_can_be_clarified():
    old = observation([transition('a', 'unknown', 'inside', '0', '1')])
    new = observation([transition('a', 'outside', 'inside', '0', '1')])
    assert not review_conflicts(old, new)
    old['objects'][0]['after']['at_rest'] = False
    assert review_conflicts(old, new)[0]['reason'] == 'review_reversed_same_frame_rest_state'


def test_review_can_move_before_endpoint_earlier_when_media_times_are_disjoint():
    old = observation([transition('a', 'inside', 'inside', '2', '3')])
    new = observation([transition('a', 'outside', 'inside', '0', '1')])
    frames = {str(i): {'media_time_ms': i * 100} for i in range(4)}
    assert review_conflicts(old, new, frames) == []
    assert review_conflicts(old, observation([transition('a', 'outside', 'inside', '3', '3')]), frames)


def test_overlapping_appearance_hints_block_two_candidate_identity():
    objects = [transition('a', 'inside', 'inside', 'f', 'g'), transition('b', 'inside', 'inside', 'f', 'g')]
    observation_value = observation(objects)
    frames = {
        'f': {'media_time_ms': 0, 'appearance_hints': [
            {'candidate_id': 'a', 'bbox_pixels': [10, 10, 110, 110]},
            {'candidate_id': 'b', 'bbox_pixels': [12, 12, 108, 108]}]},
        'g': {'media_time_ms': 1000, 'appearance_hints': []},
    }
    assert instance_conflicts(observation_value, frames)[0]['reason'] == 'overlapping_instance_hints'
    result = reconcile(observation_value, [], frames)
    assert result['status'] == 'pending'
    assert any(p['reason'] == 'overlapping_instance_hints' for p in result['pending'])


def test_regular_window_actually_supplies_timestamped_middle_frames(tmp_path, monkeypatch):
    from PIL import Image
    import app.actions as actions

    frames = [{'id': str(i), 'media_time_ms': i * 500, 'file': f'{i}.jpg'} for i in range(3)]
    for f in frames:
        Image.new('RGB', (80, 60), 'white').save(tmp_path / f['file'])
    monkeypatch.setattr(actions, 'action_frames', lambda *args, **kwargs: frames)
    plan = ScenePlan.model_validate({'phases': [{'phase': 'open', 'start_frame_id': '0',
        'end_frame_id': '2', 'interior_visible': True}], 'before_snapshot': {'frame_id': '0', 'bbox': [0, 0, 1000, 1000]},
        'after_snapshot': {'frame_id': '2', 'bbox': [0, 0, 1000, 1000]}, 'windows': [], 'uncertainties': []})
    seen = []

    def structured(client, messages, stage, schema, validate, **kwargs):
        if stage.startswith('scene_'):
            value = plan
        else:
            t = transition('a', 'absent', 'inside', '0', '2')
            t['drawer_relative_motion'] = False
            value = StateComparison.model_validate(observation([t]))
            if stage.startswith('states_'):
                assert messages[0]['content'] == actions.action_prompt('states')
                assert any('\u65f6\u5e8f\u62fc\u56fe' in p.get('text', '') for p in messages[1]['content'])
                assert any('"1"' in p.get('text', '') for p in messages[1]['content'])
                seen.append(stage)
        validate(value)
        return value

    result = actions.analyze_operation(tmp_path / 'demo.mp4', tmp_path, object(),
        {'items': [{'candidate_id': 'a', 'name': 'item', 'evidence_refs': []}]}, structured)
    assert seen and result['status'] == 'consistent'
