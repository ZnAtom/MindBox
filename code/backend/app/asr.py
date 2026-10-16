"""Isolated faster-whisper worker; no inventory or answer-file access."""
import argparse
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import time


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('audio', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--model', default='small')
    parser.add_argument('--models', type=Path, default=Path('var/models'))
    parser.add_argument('--local-files-only', action='store_true')
    args = parser.parse_args()
    os.environ.setdefault('HF_HUB_DISABLE_XET', '1')
    os.environ.setdefault('HF_HUB_DOWNLOAD_TIMEOUT', '30')
    started = time.monotonic()
    result = {'audio_sha256': sha256(args.audio), 'model': args.model,
              'model_endpoint': os.getenv('HF_ENDPOINT', 'https://huggingface.co'),
              'local_files_only': args.local_files_only,
              'language_requested': 'zh', 'device': 'cpu', 'compute_type': 'int8',
              'beam_size': 5, 'status': 'running', 'segments': []}
    save(args.output, result)
    try:
        from faster_whisper import WhisperModel
        from faster_whisper.utils import download_model
        model_path = Path(args.model) if Path(args.model).is_dir() else Path(download_model(
            args.model, cache_dir=str(args.models), local_files_only=args.local_files_only))
        result.update(model_revision=model_path.name, model_sha256=sha256(model_path / 'model.bin'),
                      libraries={p: version(p) for p in ('faster-whisper', 'ctranslate2', 'av')})
        model = WhisperModel(str(model_path), device='cpu', compute_type='int8', cpu_threads=4)
        segments, info = model.transcribe(str(args.audio), language='zh', beam_size=5,
                                         vad_filter=True, condition_on_previous_text=False)
        for segment in segments:
            result['segments'].append({'id': segment.id, 'start_ms': round(segment.start * 1000),
                'end_ms': round(segment.end * 1000), 'text': segment.text.strip(),
                'avg_logprob': segment.avg_logprob, 'no_speech_prob': segment.no_speech_prob})
        result.update(status='completed', text=''.join(s['text'] for s in result['segments']),
                      language=info.language, duration_s=info.duration,
                      elapsed_s=round(time.monotonic() - started, 3))
        save(args.output, result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
    except Exception as exc:
        result.update(status='failed', error_type=type(exc).__name__, error=str(exc)[:1200],
                      elapsed_s=round(time.monotonic() - started, 3))
        save(args.output, result)
        raise


if __name__ == '__main__':
    main()
