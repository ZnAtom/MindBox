"""Run the short tail cases ahead of the sequential runner, with separate API logs."""
import argparse
import json

from app.client import write_json
from app.pipeline import process
from model_comparison.responses_client import ResponsesClient
from model_comparison.run import ROOT, WORKSPACE, digest, gpt_settings, protected_hashes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    root = (ROOT / args.root).resolve()
    if not root.is_relative_to(ROOT / 'var/experiments'):
        parser.error('Isolated experiment root required')
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if protected_hashes() != manifest['protected_before']:
        raise RuntimeError('Protected input changed')
    client = ResponsesClient(gpt_settings(manifest['model']), root / 'gpt_pipeline/api-tail')
    for name in ('v10', 'v9', 'v8'):
        video = WORKSPACE / 'data' / (name + '.mp4')
        if digest(video) != manifest['inputs'][name]:
            raise RuntimeError('Video changed')
        print('gpt_pipeline tail', name, 'starting', flush=True)
        process(video, root / 'gpt_pipeline' / name, client, 10, resume=True)
    write_json(root / 'tail-isolation-check.json', {'unchanged': protected_hashes() == manifest['protected_before']})


if __name__ == '__main__':
    main()
