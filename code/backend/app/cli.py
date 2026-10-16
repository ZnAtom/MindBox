import argparse
from pathlib import Path

from .config import ROOT
from .media import prepare_video, video_files


def main():
    parser = argparse.ArgumentParser(description='Mind Box evidence-first video processing')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('probe', help='Probe actual API capabilities without source-video leakage')
    p.add_argument('--output', type=Path, default=ROOT / 'var/probe')
    p.add_argument('--model', help='Explicit provider model ID; never silently substituted')
    p = sub.add_parser('audit', help='Summarize run statistics and freeze output hashes')
    p.add_argument('output', type=Path)
    p = sub.add_parser('report', help='Regenerate static HTML from existing JSON without AI requests')
    p.add_argument('output', type=Path)
    for name in ('prepare', 'batch'):
        p = sub.add_parser(name)
        p.add_argument('input', type=Path)
        p.add_argument('--output', type=Path, default=ROOT / 'var/runs/baseline')
        p.add_argument('--max-frames', type=int, default=8)
        p.add_argument('--resume', action='store_true')
        p.add_argument('--model', help='Explicit provider model ID')
    args = parser.parse_args()
    if args.command == 'probe':
        from .probe import probe
        probe(args.output, args.model)
    elif args.command == 'audit':
        from .audit import audit
        audit(args.output)
    elif args.command == 'report':
        import json
        from .pipeline import render_report
        render_report(json.loads((args.output / 'summary.json').read_text(encoding='utf-8')), args.output)
    elif args.command == 'prepare':
        for path in video_files(args.input):
            meta = prepare_video(path, args.output / path.stem, max(1, min(32, args.max_frames)))
            print(path.name, meta['duration_s'], len(meta['frames']), flush=True)
    else:
        from .pipeline import batch
        batch(args.input, args.output, max(1, min(32, args.max_frames)), args.resume, args.model)


if __name__ == '__main__':
    main()
