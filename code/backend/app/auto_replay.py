"""Run the production adapter on a video in a fresh isolated database, never the live DB."""
import argparse
import hashlib
import os
from pathlib import Path

from .automatic import AutomaticRecorder, auto_settings
from .capture import Sessions
from .client import write_json
from .config import Settings
from .database import fingerprint, utcnow
from .media import iter_action_frames
from .store import Store


def prepare(video, destination, drawer):
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=False)
    store = Store(root / 'inventory.sqlite3')
    store.initialize()
    sessions = Sessions(store, root / 'sessions')
    identity, directory = sessions.create('cabinet-01')
    frames = list(iter_action_frames(Path(video).resolve(), directory, fps=8))
    for frame in frames:
        frame['received_ms'] = frame['media_time_ms']
        frame['sha256'] = hashlib.sha256((directory / frame['file']).read_bytes()).hexdigest()
    doors = [{'received_ms': 0, 'open': [i == drawer for i in range(1, 4)],
              'known': [True] * 3, 'source': 'operator_replay'}]
    manifest = {'id': identity, 'root': str(directory), 'cabinet_id': 'cabinet-01',
        'epoch': 'offline-replay', 'created_at': utcnow(), 'complete': True,
        'timestamp_basis': 'offline_video_pts', 'origin_ms': 0, 'doors': doors,
        'frames': frames, 'reasons': [], 'source_video': str(Path(video).resolve()),
        'location_source': 'operator_supplied_for_offline_test'}
    manifest['input_sha256'] = fingerprint({k: manifest[k] for k in ('frames', 'doors', 'epoch')})
    write_json(directory / 'session.json', manifest)
    sessions.update(identity, 'queued')
    return sessions, identity


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video')
    parser.add_argument('--output', required=True, help='A NEW directory for isolated test data')
    parser.add_argument('--drawer', required=True, type=int, choices=(1, 2, 3))
    args = parser.parse_args()
    sessions, identity = prepare(args.video, args.output, args.drawer)
    sessions.update(identity, 'processing')
    def replay_settings():
        Settings.load()
        return auto_settings(api_key=os.getenv('MIND_BOX_AUTO_API_KEY') or os.getenv('MIND_BOX_COMPARE_KEY'))
    try:
        AutomaticRecorder(sessions, settings_factory=replay_settings).process(identity)
    except Exception:
        sessions.update(identity, 'failed', {'message': 'Replay failed; evidence retained'})
        raise
    receipt = {'session': {k: v for k, v in sessions.get(identity).items() if k != 'root'},
        'inventory': sessions.store.get_inventory_snapshot(), 'production_inventory_modified': False,
        'note': 'Drawer supplied by operator; no hardware verification or absolute capture time.'}
    write_json(Path(args.output) / 'receipt.json', receipt)
    print(receipt['session']['state'], 'items:', len(receipt['inventory']['items']), 'output:', args.output)


if __name__ == '__main__':
    main()
