"""Supplemental inference on the exact frozen GPT dense images; no truth imported."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from app.client import Client, write_json
from app.config import ROOT, Settings
from model_comparison.run import DENSE_PROMPT, DenseResult, WORKSPACE, dense, digest, protected_hashes

SOURCE = ROOT / 'var/experiments/model-compare-20261017'
DEFAULT_ROOT = ROOT / 'var/experiments/model-compare-20261017-deepseek-dense'


def tree_hashes(root):
    return {p.relative_to(root).as_posix(): digest(p) for p in sorted(root.rglob('*')) if p.is_file()}


def check_inputs(source):
    signatures = {}
    for i in range(1, 11):
        name = f'v{i}'
        folder = source / 'gpt_dense' / name
        metadata = json.loads((folder / 'frames.json').read_text(encoding='utf-8'))
        if metadata['prompt_sha256'] != hashlib.sha256(DENSE_PROMPT.encode()).hexdigest():
            raise ValueError('Frozen prompt differs: ' + name)
        if metadata['input_sha256'] != digest(WORKSPACE / 'data' / (name + '.mp4')):
            raise ValueError('Frozen video differs: ' + name)
        signatures[name] = [{'id': f['id'], 'media_time_ms': f['media_time_ms'],
                             'sha256': digest(folder / f['file'])} for f in metadata['frames']]
    return signatures


def verify(root):
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    protected = protected_hashes()
    changed = sorted(k for k in protected.keys() | manifest['protected_before'].keys()
                     if protected.get(k) != manifest['protected_before'].get(k))
    baseline = tree_hashes(SOURCE)
    signatures = check_inputs(SOURCE)
    copied_equal = True
    for name, expected in signatures.items():
        folder = root / 'deepseek_dense' / name
        if not (folder / 'frames.json').exists():
            copied_equal = False
            continue
        metadata = json.loads((folder / 'frames.json').read_text(encoding='utf-8'))
        actual = [{'id': f['id'], 'media_time_ms': f['media_time_ms'],
                   'sha256': digest(folder / f['file'])} for f in metadata['frames']]
        copied_equal &= actual == expected
    result = {'protected_unchanged': protected == manifest['protected_before'],
              'changed_project_files': changed,
              'config_database_dependencies_unchanged': not any(not k.startswith(('app/', 'app\\')) for k in changed),
              'baseline_unchanged': baseline == manifest['baseline_before'],
              'frozen_inputs_unchanged': signatures == manifest['frame_signatures'],
              'copied_images_times_ids_equal': copied_equal}
    write_json(root / 'isolation-check.json', result)
    if not all(result[k] for k in ('config_database_dependencies_unchanged', 'baseline_unchanged',
                                  'frozen_inputs_unchanged', 'copied_images_times_ids_equal')):
        raise RuntimeError('Isolation check failed: ' + str(result))
    return result


def run(root, presentation='individual'):
    root.mkdir(parents=True, exist_ok=True)
    manifest_path = root / 'manifest.json'
    signatures = check_inputs(SOURCE)
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest['protected_before'] != protected_hashes() or manifest['baseline_before'] != tree_hashes(SOURCE):
            raise RuntimeError('Protected project or baseline changed; use a new experiment')
        if manifest['frame_signatures'] != signatures:
            raise RuntimeError('Frozen inputs changed')
        if manifest.get('presentation', 'individual') != presentation:
            raise RuntimeError('Do not change presentation within an experiment')
    else:
        write_json(manifest_path, {'created_at': datetime.now(timezone.utc).isoformat(),
            'requested_model': 'deepseek-pro', 'source': str(SOURCE),
            'presentation': presentation,
            'protected_before': protected_hashes(), 'baseline_before': tree_hashes(SOURCE),
            'frame_signatures': signatures, 'sampling_fps': 4, 'max_tokens': 8000,
            'schema_sha256': hashlib.sha256(json.dumps(DenseResult.model_json_schema(), ensure_ascii=False).encode()).hexdigest(),
            'scope': 'Fourth arm: identical frozen GPT dense JPEGs, prompt, schema and validation. No truth, DB, ASR or tasks.',
            'transport': 'Existing DeepSeek Chat Completions gateway; 2 HTTP attempts, at most 1 structured repair.'})
    settings = Settings.load()
    settings.model, settings.attempts = 'deepseek-pro', 2
    client = Client(settings, root / 'deepseek_dense/api')
    results = []
    for i in range(1, 11):
        name = f'v{i}'
        print('deepseek_dense', name, 'starting', flush=True)
        results.append(dense(WORKSPACE / 'data' / (name + '.mp4'), root / 'deepseek_dense' / name,
                             client, frozen_source=SOURCE / 'gpt_dense' / name, presentation=presentation))
        write_json(root / 'deepseek_dense/summary.json', results)
    print(json.dumps(verify(root)), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=DEFAULT_ROOT)
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--presentation', choices=['individual', 'pairs'], default='individual')
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_relative_to(ROOT / 'var/experiments') or root == SOURCE or root.is_relative_to(SOURCE):
        parser.error('Supplement must stay in a separate experiment directory')
    if args.verify_only:
        print(json.dumps(verify(root)))
    else:
        run(root, args.presentation)


if __name__ == '__main__':
    main()
