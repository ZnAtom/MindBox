"""Inspect and complete first-batch experiments without selecting a production DB."""
import argparse
import json
from pathlib import Path

from .client import Client, write_json
from .config import Settings
from .experiments import Experiment
from .identity import enrich_candidate
from .memory import ProductMemory
from .speech import SpeechService, extract_slots
from .tasks import Tasks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('experiment', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('inspect')
    p = sub.add_parser('gate')
    p.add_argument('--run', required=True)
    p = sub.add_parser('speech')
    p.add_argument('--video-hash', required=True)
    p.add_argument('--audio', type=Path, required=True)
    p.add_argument('--asr', type=Path, required=True)
    p.add_argument('--audio-offset-ms', type=int, default=0)
    p.add_argument('--removal-policy-reason', help='Explicit product policy, separately attributed from ASR intent')
    p = sub.add_parser('enrich')
    p.add_argument('--run', required=True)
    p.add_argument('--candidate', required=True)
    p.add_argument('--item', required=True)
    p = sub.add_parser('snapshot')
    p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('memory-search')
    p.add_argument('query')
    p = sub.add_parser('memory-reuse')
    p.add_argument('--item', required=True)
    p.add_argument('--product', required=True)
    p.add_argument('--item-version', type=int, required=True)
    p.add_argument('--key', required=True)
    p = sub.add_parser('memory-stage')
    p.add_argument('--item', required=True)
    p = sub.add_parser('memory-verify')
    p.add_argument('--product', required=True)
    p.add_argument('--reviewer', required=True)
    p.add_argument('--reason', required=True)
    p = sub.add_parser('task-complete')
    p.add_argument('--task', required=True)
    p.add_argument('--version', type=int, required=True)
    p.add_argument('--expiry-cutoff', required=True)
    p.add_argument('--due-at')
    args = parser.parse_args()
    context = Experiment.open(args.experiment)
    memory, tasks = ProductMemory(context), Tasks(context)
    if args.command == 'inspect':
        result = {'manifest': context.manifest, 'inventory': context.store.get_inventory_snapshot(),
                  'tasks': tasks.list(), 'reminders': tasks.scheduling.list_reminders('cabinet-01'),
                  'receipts': context.store.receipts('cabinet-01')}
    elif args.command == 'gate':
        from .coarse import CoarseIntake
        result = {'intake': CoarseIntake(context).commit(args.run),
                  'inventory': context.store.get_inventory_snapshot()}
        write_json(context.root / 'latest-state.json', result)
    elif args.command == 'speech':
        asr = json.loads(args.asr.read_text(encoding='utf-8'))
        settings = Settings.load()
        settings.model = context.manifest['model']
        slots = extract_slots(asr['text'], Client(settings, context.root / 'api'), context.root / 'asr/slots.json')
        service = SpeechService(context)
        utterance = service.ingest(args.video_hash, args.audio, asr, slots, args.audio_offset_ms)
        linked = service.link(utterance['id'])
        result = {'speech': linked, 'facts_receipt': service.apply_facts(utterance['id']),
                  'tasks': tasks.draft(utterance['id'], args.removal_policy_reason)}
        write_json(context.root / 'asr/speech-report.json', result)
    elif args.command == 'enrich':
        result = enrich_candidate(context, args.run, args.candidate, args.item)
    elif args.command == 'snapshot':
        result = memory.snapshot(args.output)
        result = {k: v for k, v in result.items() if k != 'products'}
    elif args.command == 'memory-search':
        result = memory.search(args.query)
    elif args.command == 'memory-reuse':
        item = context.store.get_item(args.item)
        found = memory.search(' '.join(str(item.get(k) or '') for k in ('name', 'brand', 'variant')))
        candidate = next((p for p in found['results'] if p['product_id'] == args.product), None)
        if not candidate:
            raise ValueError('Product was not retrieved in this experiment')
        result = memory.reuse(args.item, candidate, {k: item.get(k) for k in ('brand', 'variant')},
            args.item_version, context.store.get_inventory_snapshot()['inventory_revision'], args.key)
    elif args.command == 'memory-stage':
        item = context.store.get_item(args.item)
        result = memory.stage(args.item, {k: item[k] for k in ('name', 'category', 'description', 'brand', 'variant') if item.get(k)})
    elif args.command == 'memory-verify':
        result = memory.verify(args.product, args.reviewer, args.reason)
    else:
        result = tasks.complete(args.task, args.version, expiry_cutoff=args.expiry_cutoff, due_at=args.due_at)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
