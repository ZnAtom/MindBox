import argparse
from pathlib import Path

from .config import ROOT
from .media import prepare_video, video_files


def main():
    parser = argparse.ArgumentParser(description='Mind Box evidence-first video processing')
    sub = parser.add_subparsers(dest='command', required=True)
    from .db_cli import add_parser
    add_parser(sub)
    p = sub.add_parser('probe', help='Probe actual API capabilities without source-video leakage')
    p.add_argument('--output', type=Path, default=ROOT / 'var/probe')
    p.add_argument('--model', help='Explicit provider model ID; never silently substituted')
    p = sub.add_parser('audit', help='Summarize run statistics and freeze output hashes')
    p.add_argument('output', type=Path)
    p = sub.add_parser('report', help='Regenerate static HTML from existing JSON without AI requests')
    p.add_argument('output', type=Path)
    p = sub.add_parser('inspect-run', help='Inspect actual tools and persistence; no AI or database writes')
    p.add_argument('run', type=Path)
    p.add_argument('--output', type=Path)
    p.add_argument('--database', type=Path, help='Read actual imported runs and inventory receipts')
    p = sub.add_parser('inspect-media', help='Read actual container and stream metadata without AI')
    p.add_argument('input', type=Path)
    p.add_argument('--output', type=Path, default=ROOT / 'var/review/media-info.json')
    p = sub.add_parser('actions', help='Re-test drawer-relative actions using frozen recognition and original video')
    p.add_argument('input', type=Path)
    p.add_argument('--source-run', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--videos', nargs='+', help='Optional video stems, e.g. v8 v7')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--database', type=Path, help='Read inventory context and persist candidates; never auto-commit')
    p.add_argument('--cabinet', default='cabinet-01')
    p = sub.add_parser('evaluate-actions', help='Compare frozen candidate IDs with separate evaluation-only annotations')
    p.add_argument('run', type=Path)
    p.add_argument('--annotations', type=Path, default=ROOT.parent / 'datasets/action-regression.json')
    for name in ('prepare', 'batch'):
        p = sub.add_parser(name)
        p.add_argument('input', type=Path)
        p.add_argument('--output', type=Path, default=ROOT / 'var/runs/baseline')
        p.add_argument('--max-frames', type=int, default=8)
        p.add_argument('--resume', action='store_true')
        p.add_argument('--model', help='Explicit provider model ID')
        if name == 'batch':
            p.add_argument('--database', type=Path, help='Read inventory context and persist candidates')
            p.add_argument('--cabinet', default='cabinet-01')
    args = parser.parse_args()
    if args.command == 'db':
        from .db_cli import run
        run(args)
    elif args.command == 'probe':
        from .probe import probe
        probe(args.output, args.model)
    elif args.command == 'audit':
        from .audit import audit
        audit(args.output)
    elif args.command == 'report':
        import json
        from .pipeline import render_report
        render_report(json.loads((args.output / 'summary.json').read_text(encoding='utf-8')), args.output)
    elif args.command == 'inspect-run':
        from .review import inspect_run
        inspect_run(args.run, args.output, args.database)
    elif args.command == 'inspect-media':
        from .media import inspect_media
        from .client import write_json
        info = [inspect_media(p) for p in video_files(args.input)]
        write_json(args.output, info)
        print(f'Inspected {len(info)} videos: {args.output}', flush=True)
    elif args.command == 'evaluate-actions':
        from .evaluation import evaluate_actions
        evaluate_actions(args.run, args.annotations)
    elif args.command == 'actions':
        from .actions import action_batch
        action_batch(args.input, args.source_run, args.output, args.videos, args.resume, args.database, args.cabinet)
    elif args.command == 'prepare':
        for path in video_files(args.input):
            meta = prepare_video(path, args.output / path.stem, max(1, min(32, args.max_frames)))
            print(path.name, meta['duration_s'], len(meta['frames']), flush=True)
    else:
        from .pipeline import batch
        batch(args.input, args.output, max(1, min(32, args.max_frames)), args.resume, args.model, args.database, args.cabinet)


if __name__ == '__main__':
    main()
