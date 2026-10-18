"""Download the official sherpa-onnx Chinese keyword and Silero VAD models."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request


MODEL = 'sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20'
BASE = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/'
FILES = ('encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx',
         'decoder-epoch-13-avg-2-chunk-8-left-64.onnx',
         'joiner-epoch-13-avg-2-chunk-8-left-64.int8.onnx', 'tokens.txt', 'en.phone')
DEFAULT_DIRECTORY = Path(__file__).resolve().parent / 'models' / 'wake'


def download(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    sources = {'keyword_model': BASE + 'kws-models/' + MODEL + '.tar.bz2',
               'vad': BASE + 'asr-models/silero_vad.onnx'}
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / 'model.tar.bz2'
        print('Downloading Chinese keyword model...', flush=True)
        urllib.request.urlretrieve(sources['keyword_model'], archive)
        with tarfile.open(archive, 'r:bz2') as bundle:
            for name in FILES:
                member = bundle.getmember(MODEL + '/' + name)
                with bundle.extractfile(member) as source, (directory / name).open('wb') as target:
                    shutil.copyfileobj(source, target)
        print('Downloading voice activity detector...', flush=True)
        urllib.request.urlretrieve(sources['vad'], directory / 'silero_vad.onnx.part')
        (directory / 'silero_vad.onnx.part').replace(directory / 'silero_vad.onnx')
    sources['sha256'] = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
                         for name in (*FILES, 'silero_vad.onnx')}
    (directory / 'sources.json').write_text(json.dumps(sources, indent=2) + '\n')
    print('Models ready: ' + str(directory.resolve()))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default=DEFAULT_DIRECTORY, type=Path)
    download(parser.parse_args().output)
