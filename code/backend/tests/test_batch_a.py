import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.client import write_json
from app.coarse import CoarseIntake
from app.database import Conflict, DatabaseError
from app.enrichment import Enrichment
from app.experiments import Experiment, file_hash
from app.identity import enrich_candidate
from app.memory import ProductMemory
from app.speech import SpeechService, normalize_date, normalized_facts, validate_slots
from app.tasks import Tasks
from app.task_worker import run as worker
from test_database import make_run, plan_for


def prepared(context, root, *, change='add', source=None, revise=False, evidence='crossing', outside=False):
    make_run(root, (change,))
    result = json.loads((root / 'result.json').read_text(encoding='utf-8'))
    media = json.loads((root / 'media.json').read_text(encoding='utf-8'))
    if source:
        result['input_sha256'] = media['input_sha256'] = file_hash(source)
    result['items'][0].update(name='鸡胸肉', brand='测试品牌', variant='原味',
        description='白色包装鸡胸肉', target_kind='movable_item', localization_status='usable')
    state = lambda location, frame: {'location': location, 'frame_ids': [frame], 'interior_clear': True,
                                     'at_rest': True, 'description': location}
    transition = {'candidate_id': 'c0', 'before': state('outside' if outside else 'absent', 'f0'), 'after': state('inside', 'f1'),
                  'identity': 'supported', 'evidence': evidence, 'drawer_relative_motion': evidence == 'crossing', 'support': 'medium'}
    if outside:
        transition['before']['interior_clear'] = False
    snapshot = {'objects': [transition], 'drawer_only': False, 'uncertainties': []}
    window = {'window_id': 'w1', 'observation': copy.deepcopy(snapshot)}
    if revise:
        window['initial_observation'] = copy.deepcopy(snapshot)
        window['initial_observation']['objects'][0]['before']['location'] = 'inside'
    write_json(root / 'snapshot-states.json', snapshot)
    write_json(root / 'window-states.json', [window])
    write_json(root / 'media.json', media)
    write_json(root / 'result.json', result)
    return context.store.import_run(root)


@pytest.fixture
def scenario(tmp_path):
    video = tmp_path / 'capture.mp4'
    video.write_bytes(b'synthetic-video-fixture')
    context = Experiment.create(tmp_path, 'cold', videos=[video], known_empty=True)
    saved = prepared(context, tmp_path / 'run', source=video)
    return context, saved, video


def inserted(scenario):
    context, saved, video = scenario
    receipt = CoarseIntake(context).commit(saved['run_id'])
    assert receipt['status'] == 'committed'
    return context, saved, video, receipt['added_item_ids'][0]


def utterance(context, video, tmp_path, text, slots):
    audio = tmp_path / 'audio.wav'
    audio.write_bytes(b'synthetic-audio-fixture')
    asr = {'status': 'completed', 'audio_sha256': file_hash(audio), 'model': 'synthetic-fixture',
           'text': text, 'segments': [{'start_ms': 100, 'end_ms': 1500, 'text': text}]}
    service = SpeechService(context)
    return service, service.ingest(file_hash(video), audio, asr, slots), audio, asr


def slots(**patch):
    return {'subject_raw': '鸡胸肉', 'production_raw': None, 'expiry_raw': None,
            'shelf_life_raw': None, 'commands': [], 'uncertainties': [], **patch}


def test_coarse_then_enrichment_keeps_item_presence_count_time_and_visits(scenario):
    context, saved, _, item_id = inserted(scenario)
    before = context.store.get_item(item_id)
    assert before['name'] == '鸡胸肉' and before['stored_at'] is None
    assert before['description'] == '白色包装鸡胸肉' and before['name_kind'] == 'attribute_description'
    receipt = enrich_candidate(context, saved['run_id'], 'c0', item_id)
    after = context.store.get_item(item_id)
    assert receipt == enrich_candidate(context, saved['run_id'], 'c0', item_id)
    assert after['name'] == '鸡胸肉' and after['version'] == 2
    for key in ('id', 'presence_status', 'quantity', 'stored_at', 'storage_visits'):
        assert before[key] == after[key]
    assert len(context.store.get_inventory_snapshot()['items']) == 1


def test_consistent_alone_and_reversed_review_do_not_auto_commit(scenario, tmp_path):
    context, saved, video = scenario
    with context.store.db.transaction() as connection:
        connection.execute("UPDATE processing_runs SET artifacts_json='{}' WHERE id=?", (saved['run_id'],))
    assert CoarseIntake(context).commit(saved['run_id'])['status'] == 'pending'
    other = Experiment.create(tmp_path, 'review-conflict', videos=[video])
    saved = prepared(other, tmp_path / 'revised', source=video, revise=True)
    assert 'review_reversed_prior_observation' in CoarseIntake(other).commit(saved['run_id'])['reasons']
    assert not other.store.get_inventory_snapshot()['items']


@pytest.mark.parametrize('outside', [False, True])
def test_stable_comparison_can_commit_without_claiming_relative_crossing(tmp_path, outside):
    context = Experiment.create(tmp_path, 'comparison')
    saved = prepared(context, tmp_path / 'run', evidence='comparable_states', outside=outside)
    assessment = CoarseIntake(context).assess(saved['run_id'])
    assert assessment['status'] == 'eligible'
    assert assessment['evidence_route'] == 'stable_state_comparison'
    receipt = CoarseIntake(context).commit(saved['run_id'])
    assert receipt['status'] == 'committed'
    assert context.store.get_inventory_snapshot()['inventory_revision'] == 1


def test_gate_reports_endpoint_problems_even_when_analysis_is_pending(scenario):
    context, saved, _ = scenario
    with context.store.db.transaction() as connection:
        row = connection.execute('SELECT result_json, artifacts_json FROM processing_runs WHERE id=?',
                                 (saved['run_id'],)).fetchone()
        result, artifacts = json.loads(row['result_json']), json.loads(row['artifacts_json'])
        result['operation_analysis']['status'] = 'pending'
        artifacts['snapshot-states.json']['objects'][0]['before']['location'] = 'unknown'
        connection.execute('UPDATE processing_runs SET result_json=?, artifacts_json=? WHERE id=?',
                           (json.dumps(result), json.dumps(artifacts), saved['run_id']))
    reasons = CoarseIntake(context).assess(saved['run_id'])['reasons']
    assert 'analysis_pending_or_incomplete' in reasons
    assert 'clear_before_and_after_snapshots_required' in reasons


def test_stable_comparison_cannot_turn_unseen_absence_into_an_addition(scenario):
    context, saved, _ = scenario
    with context.store.db.transaction() as connection:
        row = connection.execute('SELECT artifacts_json FROM processing_runs WHERE id=?', (saved['run_id'],)).fetchone()
        artifacts = json.loads(row['artifacts_json'])
        artifacts['snapshot-states.json']['objects'][0]['before']['interior_clear'] = False
        connection.execute('UPDATE processing_runs SET artifacts_json=? WHERE id=?', (json.dumps(artifacts), saved['run_id']))
    assert 'clear_before_and_after_snapshots_required' in CoarseIntake(context).commit(saved['run_id'])['reasons']
    assert not context.store.get_inventory_snapshot()['items']


def test_enrichment_rejects_stale_user_membership_and_cross_item_evidence(scenario):
    context, saved, _, item_id = inserted(scenario)
    item = context.store.get_item(item_id)
    ref = item['observations'][0]['id']
    meta = {'name': {'source': 'vision', 'certainty': 'estimated', 'evidence_refs': [ref]}}
    context.store.correct_item('cabinet-01', item_id, {'name': '用户命名'}, 1, 1, 'user', 'operator')
    with pytest.raises(Conflict):
        Enrichment(context.store).apply(item_id, {'name': '旧响应'}, meta, 1, 1, 'stale')
    with pytest.raises(Conflict, match='user correction'):
        Enrichment(context.store).apply(item_id, {'name': '覆盖'}, meta, 2, 2, 'overwrite')
    with pytest.raises(DatabaseError):
        Enrichment(context.store).apply(item_id, {'presence_status': 'present'}, {}, 2, 2, 'resurrect')
    other = context.store.create_item('cabinet-01', {'name': '另一件'}, 2, 'other', 'operator')['added_item_ids'][0]
    with pytest.raises(DatabaseError, match='not associated'):
        Enrichment(context.store).apply(other, {'description': '错绑'}, {'description': meta['name']}, 1, 3, 'wrong-item')


def test_experiment_context_cold_warm_disabled_cache_and_holdout(scenario, tmp_path):
    context, saved, video, item_id = inserted(scenario)
    enrich_candidate(context, saved['run_id'], 'c0', item_id)
    memory = ProductMemory(context)
    candidate = memory.stage(item_id, {'name': '鸡胸肉', 'category': '食品', 'brand': '测试品牌', 'variant': '原味'})
    assert memory.search('鸡胸肉')['results'] == []
    memory.verify(candidate['id'], 'fixture-reviewer', 'synthetic crop fixture')
    seed = tmp_path / 'seed.json'
    memory.snapshot(seed)
    target = tmp_path / 'target.mp4'
    target.write_bytes(b'different-capture')
    warm = Experiment.create(tmp_path, 'warm', 'warm', [target], seed)
    cold = Experiment.create(tmp_path, 'new-cold', videos=[target])
    disabled = Experiment.create(tmp_path, 'disabled', 'disabled', [target])
    holdout = Experiment.create(tmp_path, 'holdout', 'holdout', [target], seed, excluded=[candidate['id']])
    assert ProductMemory(warm).search('鸡胸肉')['results']
    assert not ProductMemory(cold).search('鸡胸肉')['results']
    assert ProductMemory(disabled).search('鸡胸肉')['status'] == 'disabled'
    assert not ProductMemory(holdout).search('鸡胸肉')['results']
    assert warm.cache_key('same') != cold.cache_key('same')
    with warm.store.db.connect() as connection:
        assert connection.execute('SELECT count(*) FROM product_claims').fetchone()[0] == 4
    with pytest.raises(DatabaseError, match='test capture'):
        Experiment.create(tmp_path, 'leaking', 'warm', [video], seed)
    manifest = json.loads((cold.root / 'manifest.json').read_text(encoding='utf-8'))
    manifest['mode'] = 'warm'
    write_json(cold.root / 'manifest.json', manifest)
    with pytest.raises(Conflict):
        Experiment.open(cold.root)


def test_rag_same_product_new_physical_item_no_dates_and_conflict_rejection(scenario, tmp_path):
    context, saved, _, item_id = inserted(scenario)
    enrich_candidate(context, saved['run_id'], 'c0', item_id)
    memory = ProductMemory(context)
    profile = memory.stage(item_id, {'name': '鸡胸肉', 'category': '食品', 'brand': '测试品牌', 'variant': '原味'})
    memory.verify(profile['id'], 'fixture', 'synthetic review')
    saved2 = prepared(context, tmp_path / 'second')
    item2 = CoarseIntake(context).commit(saved2['run_id'])['added_item_ids'][0]
    candidate = memory.search('鸡胸肉')['results'][0]
    with pytest.raises(DatabaseError, match='current item'):
        memory.reuse(item2, candidate, {'brand': '测试品牌', 'variant': '原味'}, 1, 3, 'forged')
    enrich_candidate(context, saved2['run_id'], 'c0', item2)
    observed = {'brand': '测试品牌', 'variant': '原味'}
    receipt = memory.reuse(item2, candidate, observed, 2, 4, 'reuse')
    assert memory.reuse(item2, candidate, observed, 2, 4, 'reuse') == receipt
    target = context.store.get_item(item2)
    assert receipt['status'] == 'committed' and target['product_id'] == profile['id']
    assert target['id'] != item_id and target['expires_on'] is None and target['stored_at'] is None
    with pytest.raises(DatabaseError):
        memory.stage(item2, {'name': 'unsafe', 'category': '食品', 'expires_on': '2027-01-01'})
    memory.retract(profile['id'], 'wrong variant')
    assert memory.search('鸡胸肉')['results'] == []
    with pytest.raises(Conflict):
        memory.reuse(item2, candidate, observed, target['version'], 5, 'old-rag')
    refs = [o['id'] for o in target['observations'] if o['kind'] == 'crop']
    patch = {'product_id': None, 'product_match_status': 'candidate'}
    Enrichment(context.store).apply(item2, patch, {k: {'source': 'vision', 'certainty': 'estimated',
        'evidence_refs': refs} for k in patch}, target['version'], 5, 'unlink')
    assert item2 not in memory.linked_items(profile['id'])


@pytest.mark.parametrize('raw,clock,expected', [
    ('今年7月14日', None, None), ('10月21日', {'quality': 'trusted', 'capture_start': '2026-10-16T10:00:00+08:00'}, None),
    ('今年7月14日', {'quality': 'synthetic_test', 'capture_start': '2026-10-16T10:00:00+08:00'}, '2026-07-14'),
    ('2026年2月30日', None, None), ('2026-10-21', None, '2026-10-21')])
def test_year_and_invalid_dates_not_guessed(raw, clock, expected):
    assert normalize_date(raw, clock)['value'] == expected


def test_calendar_expiry_and_asr_typo_conservatism():
    facts = normalized_facts(slots(production_raw='2024年2月29日', shelf_life_raw='一年'))
    assert facts['derived_expiry'] == '2025-02-28'
    typo = normalized_facts(slots(production_raw='2026年7月14日', shelf_life_raw='保证期一年'))
    assert typo['derived_expiry'] is None and 'shelf_life_needs_review' in typo['missing']
    with pytest.raises(DatabaseError):
        validate_slots('这个鸡胸肉', slots(expiry_raw='2026年10月21日'))


def test_speech_item_link_dates_idempotency_and_final_revision(scenario, tmp_path):
    context, saved, video, item_id = inserted(scenario)
    enrich_candidate(context, saved['run_id'], 'c0', item_id)
    text = '这个鸡胸肉2026年10月21日过期，提醒我吃掉它'
    parsed = slots(expiry_raw='2026年10月21日', commands=[{'kind': 'before_expiry', 'raw': '提醒我吃掉它'}])
    service, original, audio, asr = utterance(context, video, tmp_path, text, parsed)
    assert service.link(original['id'])['item_id'] == item_id
    receipt = service.apply_facts(original['id'])
    assert service.apply_facts(original['id']) == receipt
    assert context.store.get_item(item_id)['expires_on'] == '2026-10-21'
    assert service.ingest(file_hash(video), audio, asr, parsed)['id'] == original['id']
    drafts = Tasks(context).draft(original['id'])
    assert len(drafts) == 1 and drafts == Tasks(context).draft(original['id'])
    changed = {**asr, 'text': '鸡胸肉2026年10月22日过期'}
    with pytest.raises(Conflict, match='revalidation'):
        service.ingest(file_hash(video), audio, changed, slots(expiry_raw='2026年10月22日'))
    assert len(Tasks(context).list()) == 1


def test_speech_does_not_bind_by_time_alone_or_to_existing_drink(scenario, tmp_path):
    context, saved, video, item_id = inserted(scenario)
    enrich_candidate(context, saved['run_id'], 'c0', item_id)
    service, original, _, _ = utterance(context, video, tmp_path, '这个可乐10月21日过期',
                                      slots(subject_raw='可乐', expiry_raw='10月21日'))
    assert service.link(original['id'])['link_status'] == 'pending'
    assert service.apply_facts(original['id'])['status'] == 'pending'
    assert context.store.get_item(item_id)['expires_on'] is None


def test_late_speech_link_and_date_refresh_the_same_task_draft(scenario, tmp_path):
    context, saved, video, item_id = inserted(scenario)
    text = '鸡胸肉2026年10月21日过期，提醒我吃掉它'
    service, utter, _, _ = utterance(context, video, tmp_path, text,
        slots(expiry_raw='2026年10月21日', commands=[{'kind': 'before_expiry', 'raw': '提醒我吃掉它'}]))
    tasks = Tasks(context)
    first = tasks.draft(utter['id'])[0]
    first_data = json.loads(first['data_json'])
    assert first['item_id'] is None and 'item_link' in first_data['missing']

    linked = service.link(utter['id'])
    assert linked['item_id'] == item_id
    second = tasks.list()[0]
    assert second['id'] == first['id'] and second['version'] == 2 and second['item_id'] == item_id
    assert 'item_link' not in json.loads(second['data_json'])['missing']

    service.apply_facts(utter['id'])
    third = tasks.list()[0]
    third_data = json.loads(third['data_json'])
    assert third['id'] == first['id'] and third['version'] == 3
    assert 'item_link' not in third_data['missing'] and 'expiry_date' not in third_data['missing']
    assert len(tasks.draft(utter['id'])) == 1
    assert tasks.list()[0]['version'] == 3


def test_real_scheduler_countdown_and_no_fake_delivery(scenario, tmp_path):
    context, saved, video, item_id = inserted(scenario)
    enrich_candidate(context, saved['run_id'], 'c0', item_id)
    tomorrow = datetime.now(timezone.utc) + timedelta(days=2)
    date = tomorrow.date().isoformat()
    text = f'鸡胸肉{date}过期，提醒我吃掉它'
    service, utter, _, _ = utterance(context, video, tmp_path, text,
        slots(expiry_raw=date, commands=[{'kind': 'before_expiry', 'raw': '提醒我吃掉它'}]))
    service.link(utter['id'])
    service.apply_facts(utter['id'])
    tasks = Tasks(context)
    draft = tasks.draft(utter['id'])[0]
    cutoff = tomorrow.isoformat()
    due = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    result = tasks.complete(draft['id'], 1, expiry_cutoff=cutoff, due_at=due)
    assert tasks.complete(draft['id'], 1, expiry_cutoff=cutoff, due_at=due)['id'] == result['id']
    # A fresh scanner instance reconstructs deadlines from the business DB.
    report = worker(Experiment.open(context.root), 2)
    assert report['reminders'][0]['status'] == 'triggered'
    assert report['reminders'][0]['deliveries'][0]['state'] == 'queued'
    assert report['reminders'][0]['deliveries'][0]['played_at'] is None
    with context.store.db.connect() as connection:
        assert connection.execute("SELECT count(*) FROM jobs WHERE kind='reminder.deliver'").fetchone()[0] == 1


def test_missing_year_does_not_activate_reminder(scenario, tmp_path):
    context, saved, video, item_id = inserted(scenario)
    enrich_candidate(context, saved['run_id'], 'c0', item_id)
    service, utter, _, _ = utterance(context, video, tmp_path, '鸡胸肉10月21日过期提醒我吃掉它',
        slots(expiry_raw='10月21日', commands=[{'kind': 'before_expiry', 'raw': '提醒我吃掉它'}]))
    service.link(utter['id'])
    assert service.apply_facts(utter['id'])['status'] == 'needs_input'
    task = Tasks(context).draft(utter['id'])[0]
    with pytest.raises(DatabaseError, match='recorded expiry'):
        Tasks(context).complete(task['id'], 1, expiry_cutoff='2030-10-22T00:00:00+08:00', due_at='2030-10-20T09:00:00+08:00')
    assert Tasks(context).scheduling.list_reminders('cabinet-01') == []


def test_conditional_rule_requires_committed_remove_clock_and_deduplicates(scenario, tmp_path):
    context, saved, video, item_id = inserted(scenario)
    enrich_candidate(context, saved['run_id'], 'c0', item_id)
    service, utter, _, _ = utterance(context, video, tmp_path, '鸡胸肉2026年10月21日过期，过期后取出时提醒我',
        slots(expiry_raw='2026年10月21日', commands=[{'kind': 'after_expiry_remove', 'raw': '过期后取出时提醒我'}]))
    service.link(utter['id'])
    service.apply_facts(utter['id'])
    tasks = Tasks(context)
    draft = tasks.draft(utter['id'])[0]
    tasks.complete(draft['id'], 1, expiry_cutoff='2026-10-22T00:00:00+08:00', at='2026-10-20T12:00:00+08:00')
    remove = prepared(context, tmp_path / 'remove', change='remove', source=None)
    plan = plan_for(remove, ('remove',), 'remove')
    plan['decisions'][0].update(item_id=item_id, expected_version=3)
    context.store.commit_inventory(plan)
    with context.store.db.connect() as connection:
        event = connection.execute('SELECT id FROM verified_events WHERE commit_id=(SELECT id FROM inventory_commits WHERE idempotency_key=?)', ('remove',)).fetchone()[0]
        capture_hash = connection.execute('SELECT input_sha256 FROM media_captures m JOIN operations o ON o.capture_id=m.id WHERE o.id=?', (remove['operation_id'],)).fetchone()[0]
    assert tasks.evaluate_remove(event)['reason'] == 'no_trusted_event_clock'
    context.manifest['videos'].append({'path': 'synthetic-remove-fixture', 'sha256': capture_hash})
    context.manifest['capture_clock'] = {'quality': 'synthetic_test', 'capture_start': '2026-10-23T12:00:00+08:00'}
    context.save_manifest()
    first = tasks.evaluate_remove(event, at='2026-10-23T12:00:03+08:00')
    second = tasks.evaluate_remove(event, at='2026-10-23T12:00:04+08:00')
    assert len(first['reminders']) == 1 and first['reminders'] == second['reminders']
    assert first['task_completed'] is False
    scanned = tasks.scan_rules(at='2026-10-23T12:00:05+08:00')
    assert scanned[0]['reminders'] == first['reminders']


def test_policy_rule_is_distinct_from_heard_speech_and_cancellable(scenario, tmp_path):
    context, saved, video, item_id = inserted(scenario)
    enrich_candidate(context, saved['run_id'], 'c0', item_id)
    service, utter, _, _ = utterance(context, video, tmp_path, '鸡胸肉10月21日过期提醒我吃掉它',
        slots(expiry_raw='10月21日', commands=[{'kind': 'before_expiry', 'raw': '提醒我吃掉它'}]))
    service.link(utter['id'])
    tasks = Tasks(context)
    assert len(tasks.draft(utter['id'])) == 1
    drafts = tasks.draft(utter['id'], removal_policy_reason='explicit fixture product policy')
    assert len(drafts) == 2
    policy = next(d for d in drafts if d['kind'] == 'after_expiry_remove')
    assert json.loads(policy['data_json'])['origin'] == 'configured_policy'
    assert tasks.cancel(policy['id'], 1)['status'] == 'canceled'
    assert tasks.cancel(policy['id'], 1)['version'] == 2


def test_holdout_excludes_duplicate_view_under_another_product_id(scenario, tmp_path):
    context, saved, _, item_id = inserted(scenario)
    enrich_candidate(context, saved['run_id'], 'c0', item_id)
    memory = ProductMemory(context)
    for name in ('鸡胸肉', '相同包装别名'):
        product = memory.stage(item_id, {'name': name, 'category': '食品'})
        memory.verify(product['id'], 'fixture', 'synthetic same-view example')
    seed = tmp_path / 'dupes.json'
    snapshot = memory.snapshot(seed)
    held = Experiment.create(tmp_path, 'held-out', 'holdout', seed=seed, excluded=[snapshot['products'][0]['id']])
    assert ProductMemory(held).snapshot()['products'] == []


def test_late_enrichment_of_removed_item_never_resurrects_it(scenario, tmp_path):
    context, saved, _, item_id = inserted(scenario)
    removed = prepared(context, tmp_path / 'removed', change='remove')
    plan = plan_for(removed, ('remove',), 'remove-late')
    plan['decisions'][0].update(item_id=item_id, expected_version=1)
    context.store.commit_inventory(plan)
    enrich_candidate(context, saved['run_id'], 'c0', item_id)
    assert context.store.get_item(item_id)['presence_status'] == 'removed'
    assert context.store.get_inventory_snapshot()['items'] == []


def test_uncertain_date_does_not_become_a_recorded_fact():
    facts = normalized_facts(slots(expiry_raw='2026年10月21日', uncertainties=['ASR date uncertain']))
    assert facts['expiry']['value'] is None and 'semantic_needs_review' in facts['missing']


def test_speech_cli_orchestration_preserves_item_and_connects_services(scenario, tmp_path, monkeypatch):
    from app.batch_a import speech_pass
    from app.config import Settings
    context, saved, video, item_id = inserted(scenario)
    enrich_candidate(context, saved['run_id'], 'c0', item_id)
    output = tmp_path / 'run'
    audio = output / 'audio.wav'
    audio.write_bytes(b'synthetic CLI audio fixture')
    media = json.loads((output / 'media.json').read_text(encoding='utf-8'))
    media['audio'] = {'first_media_time_ms': 0}
    write_json(output / 'media.json', media)
    text = '鸡胸肉2026年10月21日过期提醒我吃掉它'
    transcript = {'status': 'completed', 'model': 'synthetic-cli-fixture', 'audio_sha256': file_hash(audio),
                  'text': text, 'segments': [{'start_ms': 100, 'end_ms': 1500}]}

    def asr_process(command, **kwargs):
        write_json(command[command.index('--output') + 1], transcript)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr('app.batch_a.subprocess.run', asr_process)
    monkeypatch.setattr('app.speech.extract_slots', lambda *args: slots(expiry_raw='2026年10月21日',
        commands=[{'kind': 'before_expiry', 'raw': '提醒我吃掉它'}]))
    args = SimpleNamespace(asr_python=tmp_path / 'asr.exe', asr_models=tmp_path,
                           video=video, removal_policy_reason=None)
    result = speech_pass(context, Settings('unused', '', 'test'), args, output)
    assert result['status'] == 'completed' and result['utterance']['item_id'] == item_id
    assert result['facts_receipt']['status'] == 'committed'
    assert result['tasks'][0]['status'] == 'needs_input'
    assert len(context.store.get_inventory_snapshot()['items']) == 1
