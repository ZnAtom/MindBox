"""Assisted evidence review demo, separate from automatic action acceptance."""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.client import Client, write_json
from app.coarse import CoarseIntake
from app.config import Settings
from app.database import fingerprint
from app.experiments import Experiment, file_hash
from app.identity import enrich_candidate
from app.memory import ProductMemory
from app.memory_tools import retrieve
from app.scheduling import Scheduling
from app.speech import SpeechService
from app.task_worker import run as run_worker
from app.tasks import Tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--video', type=Path, required=True)
    parser.add_argument('--target-video', type=Path, required=True)
    parser.add_argument('--candidate', required=True)
    parser.add_argument('--review-note', required=True)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--asr', type=Path, required=True)
    parser.add_argument('--slots', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--native-tools', action='store_true')
    parser.add_argument('--removal-policy-reason')
    args = parser.parse_args()
    context = Experiment.create(args.output.parent, args.output.name, videos=[args.video])
    imported = context.store.import_run(args.source)
    automatic = CoarseIntake(context).assess(imported['run_id'])
    result = json.loads((args.source / 'result.json').read_text(encoding='utf-8'))
    analysis = result['operation_analysis']
    if analysis['net_changes'] != [] and not any(n['candidate_id'] == args.candidate and n['change'] == 'add' for n in analysis['net_changes']):
        raise ValueError('Reviewed candidate must be a supported addition in this source run')
    if imported.get('pending_count'):
        context.store.resolve_pending(imported['run_id'], args.review_note, 'agent:evidence-review')
    candidate = next(i for i in result['items'] if i['candidate_id'] == args.candidate)
    receipt = context.store.commit_inventory({**{k: imported[k] for k in ('operation_id', 'run_id', 'base_revision')},
        'idempotency_key': 'assisted-coarse', 'reviewed_by': 'agent:evidence-review', 'coarse_only': True,
        'decisions': [{'candidate_id': args.candidate, 'action': 'add', 'evidence_refs': candidate['evidence_refs']}]})
    item_id = receipt['added_item_ids'][0]
    coarse = context.store.get_item(item_id)
    enriched = enrich_candidate(context, imported['run_id'], args.candidate, item_id)
    full = context.store.get_item(item_id)
    service = SpeechService(context)
    asr = json.loads(args.asr.read_text(encoding='utf-8'))
    slots = json.loads(args.slots.read_text(encoding='utf-8'))['slots']
    utterance = service.ingest(file_hash(args.video), args.audio, asr, slots)
    linked = service.link(utterance['id'])
    facts = service.apply_facts(utterance['id'])
    drafts = Tasks(context).draft(utterance['id'], args.removal_policy_reason)
    memory = ProductMemory(context)
    # The reviewer inspected the actual package crop. Batch/expiry facts never enter this profile.
    profile = memory.stage(item_id, {k: full[k] for k in ('name', 'category', 'brand', 'description') if full.get(k)})
    memory.verify(profile['id'], 'agent:evidence-review', args.review_note + '; reusable name/category/brand only; variant unknown')
    seed = context.root / 'verified-products.json'
    snapshot = memory.snapshot(seed)
    comparisons = {}
    for mode in ('cold', 'warm', 'holdout', 'disabled'):
        target = Experiment.create(context.root / 'comparisons', mode, mode, [args.target_video],
            seed=seed if mode in ('warm', 'holdout') else None,
            excluded=[profile['id']] if mode == 'holdout' else [])
        comparisons[mode] = ProductMemory(target).search('鸡胸肉 食品')
        comparisons[mode]['cache_key'] = target.cache_key('same-query')
    tool_result = None
    if args.native_tools:
        warm = Experiment.open(context.root / 'comparisons/warm')
        settings = Settings.load()
        settings.model = warm.manifest['model']
        tool_result = retrieve(Client(settings, warm.root / 'api'), ProductMemory(warm),
                               {'name': '鸡胸肉', 'category': '食品'}, warm.root / 'product-tool-trace.json')
    timer = Experiment.create(context.root / 'comparisons', 'countdown', 'disabled')
    now = datetime.now(timezone.utc)
    timer_receipt = Scheduling(timer.store.db).create_reminder('cabinet-01', '结构化倒计时验收（模拟，不来自v10语音）',
        (now + timedelta(seconds=2)).isoformat(), 'countdown-demo')
    countdown = run_worker(timer, 4, timer.root / 'scheduler-report.json')
    report = {'assisted': True, 'review': {'reviewer': 'agent:evidence-review', 'reason': args.review_note},
        'automatic_assessment': automatic, 'coarse_commit_receipt': receipt, 'coarse_item': coarse,
        'identity_receipt': enriched, 'enriched_item': full, 'same_item_id': coarse['id'] == full['id'],
        'speech': linked, 'facts_receipt': facts, 'task_drafts': drafts,
        'memory_snapshot': {'hash': snapshot['content_hash'], 'products': len(snapshot['products']), 'views': len(snapshot['products'][0]['examples'])},
        'retrieval_comparison': comparisons, 'native_tool_result': tool_result,
        'countdown': {'created': timer_receipt, 'result': countdown},
        'inventory': context.store.get_inventory_snapshot(),
        'limitations': ['Assisted action review is not automatic success',
            'Development sample, not an independent holdout', 'No verified recording year or reminder time',
            'Warm retrieval is a product candidate, not proof of the same SKU or physical item',
            'Countdown delivery only queued; no web/device playback confirmed']}
    write_json(context.root / 'acceptance-report.json', report)
    print(json.dumps({'report': str(context.root / 'acceptance-report.json'), 'item_id': item_id,
        'revision': report['inventory']['inventory_revision'], 'speech_link': linked['link_status'],
        'facts': facts['status'], 'drafts': len(drafts),
        'countdown': countdown['reminders'][0]['status']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
