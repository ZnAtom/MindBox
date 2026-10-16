"""Explicit, isolated inventory/knowledge/cache contexts for reproducible runs."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

from .client import write_json
from .database import Conflict, DatabaseError, encode, fingerprint, new_id, utcnow
from .store import Store


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Experiment:
    root: Path
    manifest: dict
    store: Store

    @classmethod
    def create(cls, parent, name, mode='cold', videos=(), seed=None, excluded=(), clock=None,
               known_empty=False, model='deepseek-pro', max_frames=10):
        if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', name):
            raise DatabaseError('Use a simple unique experiment name')
        if mode not in ('cold', 'warm', 'holdout', 'disabled'):
            raise DatabaseError('Unknown memory mode')
        if (mode == 'warm' and seed is None) or (mode in ('cold', 'disabled') and seed is not None):
            raise DatabaseError('Cold/disabled forbid seeds; warm requires an explicit snapshot')
        inputs = [{'path': str(Path(p).resolve()), 'sha256': file_hash(p)} for p in videos]
        root = Path(parent).resolve() / name
        root.mkdir(parents=True, exist_ok=False)
        for folder in ('api', 'asr', 'outputs', 'indexes'):
            (root / folder).mkdir()
        code_root = Path(__file__).parent
        code_hashes = {str(p.relative_to(code_root)): file_hash(p) for p in sorted(code_root.rglob('*.py'))}
        prompt_hashes = {p.name: file_hash(p) for p in sorted((code_root / 'prompts').glob('*.txt'))}
        manifest = {'schema': 1, 'scope_id': new_id(), 'mode': mode, 'created_at': utcnow(),
            'database': 'inventory.sqlite3', 'inventory_baseline': 'known_empty' if known_empty else 'unknown',
            'videos': inputs, 'capture_clock': clock, 'web_enabled': False, 'model': model,
            'code_hashes': code_hashes, 'prompt_hashes': prompt_hashes, 'excluded_products': sorted(excluded),
            'budget': {'max_frames': max_frames, 'cloud_concurrency': 1, 'retrieval_limit': 3},
            'knowledge_seed_hash': None, 'knowledge_snapshot': None}
        store = Store(root / manifest['database'])
        store.initialize(known_empty=known_empty)
        with store.db.transaction() as connection:
            connection.execute('INSERT INTO experiment_context VALUES (1, ?, ?, ?)',
                               (manifest['scope_id'], mode, encode(manifest)))
        context = cls(root, manifest, store)
        if seed is not None:
            from .memory import ProductMemory
            snapshot = json.loads(Path(seed).read_text(encoding='utf-8'))
            if fingerprint(snapshot['products']) != snapshot['content_hash']:
                raise Conflict('Knowledge snapshot checksum mismatch')
            ProductMemory(context).import_snapshot(snapshot, excluded, {i['sha256'] for i in inputs})
            manifest['knowledge_seed_hash'] = snapshot['content_hash']
        from .memory import ProductMemory
        manifest['knowledge_snapshot'] = ProductMemory(context).snapshot()['content_hash']
        context.save_manifest()
        return context

    @classmethod
    def open(cls, root):
        root = Path(root).resolve()
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('database') != 'inventory.sqlite3':
            raise DatabaseError('Experiment cannot select an external database')
        store = Store(root / 'inventory.sqlite3')
        if store.db.path != root / 'inventory.sqlite3' or store.db.assets.resolve() != root / 'inventory-assets':
            raise DatabaseError('Experiment database/assets must stay inside their own directory')
        with store.db.connect() as connection:
            row = connection.execute('SELECT * FROM experiment_context WHERE singleton = 1').fetchone()
            if not row or (row['scope_id'], row['mode']) != (manifest['scope_id'], manifest['mode']):
                raise Conflict('Experiment namespace does not match its database')
            if json.loads(row['manifest_json']) != manifest:
                raise Conflict('Manifest changed outside the experiment service')
        return cls(root, manifest, store)

    def save_manifest(self):
        with self.store.db.transaction() as connection:
            connection.execute('UPDATE experiment_context SET manifest_json = ? WHERE singleton = 1',
                               (encode(self.manifest),))
        write_json(self.root / 'manifest.json', self.manifest)

    def retrieval_context(self):
        from .memory import ProductMemory
        return {'scope_id': self.manifest['scope_id'], 'mode': self.manifest['mode'],
                'snapshot': ProductMemory(self).snapshot()['content_hash']}

    def cache_key(self, evidence, references=(), config=None):
        return fingerprint({'context': self.retrieval_context(), 'evidence': evidence,
                            'references': references, 'config': config})
