"""Explicit isolated entry point for the first offline implementation batch."""
import argparse
import json
from pathlib import Path
import subprocess

from .client import APIError, Client, write_json
from .config import Settings
from .experiments import Experiment, file_hash
from .database import fingerprint
from .pipeline import process


def speech_pass(context, settings, args, output):
    from .speech import SpeechService, extract_slots
    from .tasks import Tasks
    transcript_path = context.root / 'asr/transcript.json'
    audio = output / 'audio.wav'
    client = Client(settings, context.root / 'api')
    try:
        command = [str(args.asr_python.resolve()), '-m', 'app.asr', str(audio), '--output', str(transcript_path),
                   '--models', str(args.asr_models.resolve()), '--model', 'small', '--local-files-only']
        asr_process = subprocess.run(command, capture_output=True)
        if asr_process.returncode or not transcript_path.exists():
            return {'status': 'failed', 'stage': 'local_asr', 'exit_code': asr_process.returncode,
                    'error_record': str(transcript_path)}
        asr = json.loads(transcript_path.read_text(encoding='utf-8'))
        slots = extract_slots(asr['text'], client, context.root / 'asr/slots.json')
        service = SpeechService(context)
        media = json.loads((output / 'media.json').read_text(encoding='utf-8'))
        utterance = service.ingest(file_hash(args.video), audio, asr, slots, media['audio']['first_media_time_ms'])
        return {'status': 'completed', 'utterance': service.link(utterance['id']),
                'facts_receipt': service.apply_facts(utterance['id']),
                'tasks': Tasks(context).draft(utterance['id'], args.removal_policy_reason)}
    except (OSError, ValueError, KeyError, APIError) as exc:
        return {'status': 'failed', 'stage': 'speech_pass', 'error_type': type(exc).__name__, 'error': client.safe(exc)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--mode', choices=('cold', 'warm', 'holdout', 'disabled'), default='cold')
    parser.add_argument('--seed', type=Path)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--asr-python', type=Path, help='Python from the separate faster-whisper environment')
    parser.add_argument('--asr-models', type=Path, default=Path('var/models'))
    parser.add_argument('--removal-policy-reason')
    args = parser.parse_args()
    settings = Settings.load()
    settings.model = 'deepseek-pro'
    if args.resume:
        context = Experiment.open(args.root)
    else:
        context = Experiment.create(args.root.parent, args.root.name, args.mode,
                                    [args.video], args.seed, model=settings.model)
    if file_hash(args.video) not in {v['sha256'] for v in context.manifest['videos']}:
        raise ValueError('Video is outside the frozen experiment inputs')
    output = context.root / 'outputs' / args.video.stem
    with context.store.db.connect() as connection:
        committed = connection.execute("SELECT 1 FROM operations p JOIN media_captures m ON m.id=p.capture_id "
            "WHERE p.state='committed' AND m.input_sha256=?", (file_hash(args.video),)).fetchone()
    if committed:
        print('already_committed; inspect the experiment for the current inventory', flush=True)
        return
    inventory = context.store.get_inventory_snapshot()
    frozen = output / 'result.json'
    if args.resume and frozen.exists():
        previous = json.loads(frozen.read_text(encoding='utf-8'))
        previous_inventory = previous.get('inventory_context')
        if previous_inventory and previous_inventory['inventory_revision'] == inventory['inventory_revision']:
            inventory = previous_inventory
    result = process(args.video, output, Client(settings, context.root / 'api'), 10,
                     resume=args.resume, initial_inventory=inventory)
    saved = context.store.import_run(output, source_key=str(output.resolve()) + ':' + fingerprint(result))
    from .coarse import CoarseIntake
    intake = CoarseIntake(context).commit(saved['run_id']) if result['status'] == 'completed' else {'status': 'failed'}
    enrichment = None
    if intake['status'] == 'committed':
        from .identity import enrich_candidate
        assessment = CoarseIntake(context).assess(saved['run_id'])
        enrichment = enrich_candidate(context, saved['run_id'], assessment['candidate_id'], intake['added_item_ids'][0])
    speech = {'status': 'not_requested'}
    if args.asr_python:
        speech = speech_pass(context, settings, args, output)
    limitations = list(result.get('limitations', []))
    if speech['status'] == 'completed':
        limitations = [entry for entry in limitations if entry != 'no ASR transcript']
    report = {'recognition_status': result['status'], 'import': saved, 'intake': intake,
              'enrichment': enrichment, 'speech': speech,
              'operation_state': context.store.get_operation(saved['operation_id'])['state'],
              'inventory': context.store.get_inventory_snapshot(), 'limitations': limitations,
              'import_receipt_scope': 'State at import time; operation_state and inventory are current after all stages.'}
    write_json(context.root / 'report.json', report)
    print(report['recognition_status'], intake['status'], flush=True)


if __name__ == '__main__':
    main()
