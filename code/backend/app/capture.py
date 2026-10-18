"""Archive bridge JPEGs on the host clock, independently of model latency and UI."""
import hashlib
import json
from pathlib import Path
import time
from uuid import uuid4

import httpx

from .client import write_json
from .database import encode, utcnow
from .recognition import recognition_lock


class Sessions:
    def __init__(self, store, root):
        self.store, self.root = store, Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, cabinet):
        identity = 'session-' + uuid4().hex
        directory = self.root / identity
        directory.mkdir()
        now = utcnow()
        with self.store.db.transaction() as db:
            db.execute('INSERT INTO automatic_sessions VALUES (?,?,?,?,?,?,?)',
                       (identity, cabinet, 'capturing', str(directory), now, now, '{}'))
        return identity, directory

    def update(self, identity, state, detail=None):
        with self.store.db.transaction() as db:
            db.execute('UPDATE automatic_sessions SET state=?,updated_at=?,detail_json=? WHERE id=?',
                       (state, utcnow(), encode(detail or {}), identity))

    def get(self, identity):
        with self.store.db.connect() as db:
            row = db.execute('SELECT * FROM automatic_sessions WHERE id=?', (identity,)).fetchone()
        if row is None:
            from .database import NotFound
            raise NotFound('Session not found')
        result = dict(row)
        result['detail'] = json.loads(result.pop('detail_json'))
        return result

    def list(self, cabinet='cabinet-01', limit=30):
        with self.store.db.connect() as db:
            rows = db.execute('SELECT id FROM automatic_sessions WHERE cabinet_id=? '
                              'ORDER BY created_at DESC,id LIMIT ?', (cabinet, limit)).fetchall()
        return [self.get(row['id']) for row in rows]

    def recover(self):
        with self.store.db.transaction() as db:
            db.execute("UPDATE automatic_sessions SET state='pending',updated_at=?,detail_json=? "
                       "WHERE state='capturing'", (utcnow(), encode({'reasons': ['capture_process_interrupted']})))
            db.execute("UPDATE automatic_sessions SET state='queued',updated_at=? WHERE state='processing'", (utcnow(),))

    def pause_queued(self):
        with self.store.db.transaction() as db:
            db.execute("UPDATE automatic_sessions SET state='pending',updated_at=?,detail_json=? "
                       "WHERE state='queued'", (utcnow(), encode({'reasons': ['recognition_disabled']})))


class BridgeCapture:
    def __init__(self, sessions, bridge='http://127.0.0.1:8765', cabinet='cabinet-01',
                 *, preroll_ms=2000, tail_ms=2000, max_seconds=None, max_bytes=512 * 1024 * 1024,
                 clock=time.monotonic, recognition=None):
        self.sessions, self.cabinet, self.bridge = sessions, cabinet, bridge.rstrip('/')
        self.preroll_ms, self.tail_ms = preroll_ms, tail_ms
        self.max_seconds, self.max_bytes, self.clock = max_seconds, max_bytes, clock
        self.recognition = recognition
        self.waiting_for_closed = recognition is not None
        self.active = None
        self.armed = False
        self.last_epoch = None
        self.status = {'connected': False, 'state': 'waiting'}

    def reason(self, value):
        if self.active and value not in self.active['reasons']:
            self.active['reasons'].append(value)

    def finish(self, reason=None):
        if not self.active:
            return
        self.reason(reason) if reason else None
        data = self.active
        self.active = None
        if len(data['frames']) < 3:
            data['reasons'].append('insufficient_frames')
        data['complete'] = not data['reasons']
        data['finished_at'] = utcnow()
        data['input_sha256'] = hashlib.sha256(encode({
            'frames': data['frames'], 'doors': data['doors'], 'epoch': data['epoch']}).encode()).hexdigest()
        directory = Path(data['root'])
        write_json(directory / 'session.json', data)
        with self.sessions.store.db.connect() as db:
            backlog = db.execute("SELECT count(*) FROM automatic_sessions WHERE state IN ('queued','processing')").fetchone()[0]
        if data['complete'] and backlog >= 10:
            data['reasons'].append('analysis_queue_full')
        self.sessions.update(data['id'], 'queued' if data['complete'] and not data['reasons'] else 'pending',
                             {'frames': len(data['frames']), 'reasons': data['reasons']})
        self.status = {'connected': True, 'state': 'waiting', 'last_session_id': data['id']}

    def step(self, door, cache, load_frame):
        """A bridge snapshot and an injected JPEG reader also permit offline replay tests."""
        with recognition_lock(self.recognition):
            if self.recognition and not self.recognition.enabled:
                self.pause_recognition()
                return
            self._step(door, cache, load_frame)

    def pause_recognition(self):
        self.finish('recognition_disabled')
        self.armed = False
        self.waiting_for_closed = True
        self.status = {'connected': False, 'state': 'disabled'}

    def _step(self, door, cache, load_frame):
        now = self.clock()
        message = door.get('state') or {}
        frames = cache.get('frames', [])
        connected = door.get('connected') and door.get('cabinet_id') == self.cabinet
        if not connected or not frames:
            self.finish('bridge_or_camera_disconnected')
            self.armed = False
            self.status = {'connected': False, 'state': 'waiting'}
            return
        epoch = cache['epoch']
        latest_ms = frames[-1]['received_ms']
        if self.last_epoch != epoch:
            self.finish('camera_epoch_changed')
            self.armed = False
            self.last_epoch = epoch
        if time.time() * 1000 - frames[-1]['received_unix_ms'] > 1500:
            self.finish('camera_stalled')
            self.armed = False
            return
        opened, known = message.get('open', []), message.get('known', [True] * 3)
        reliable = len(opened) == 3 and len(known) == 3 and all(known)
        closed = reliable and not any(opened)
        drawers = [i + 1 for i, value in enumerate(opened) if value]
        if self.waiting_for_closed:
            if not closed:
                self.status = {'connected': True, 'state': 'waiting_for_close'}
                return
            self.waiting_for_closed = False
        if self.active and self.active['boot_id'] != message.get('boot_id'):
            self.finish('door_process_restarted')
            self.armed = False
        if not self.active and closed:
            self.armed = True
        if not self.active and reliable and len(drawers) == 1:
            identity, directory = self.sessions.create(self.cabinet)
            self.active = {'id': identity, 'root': str(directory), 'cabinet_id': self.cabinet,
                'created_at': utcnow(), 'epoch': epoch, 'boot_id': message.get('boot_id'),
                'timestamp_basis': 'host_monotonic_arrival', 'frames': [], 'doors': [], 'reasons': [],
                'bytes': 0, 'started_monotonic': now, 'close_at': None,
                'trigger_monotonic': door.get('received_monotonic', now),
                'capture_policy': {'preroll_ms': self.preroll_ms, 'tail_ms': self.tail_ms},
                'last_generation': 0, 'last_saved_ms': None, 'origin_ms': None}
            if not self.armed:
                self.reason('opening_without_observed_closed_state')
            if message.get('source') != 'vision':
                self.reason('non_visual_capture')
            self.armed = False
        data = self.active
        self.status = {'connected': True, 'state': 'capturing' if data else 'waiting',
                       'session_id': data['id'] if data else None}
        if not data:
            return
        if not reliable or len(drawers) > 1:
            self.reason('drawer_identity_uncertain')
        if message.get('source') != 'vision':
            self.reason('non_visual_capture')
        signature = {'open': opened, 'known': known, 'source': message.get('source')}
        if not data['doors'] or any(data['doors'][-1][key] != value for key, value in signature.items()):
            data['doors'].append({**signature, 'received_ms': latest_ms})
        if closed:
            if data['close_at'] is None:
                data['close_at'] = now
        else:
            data['close_at'] = None
        for frame in frames:
            generation, ms = frame['generation'], frame['received_ms']
            if generation <= data['last_generation']:
                continue
            data['last_generation'] = generation
            if ms < latest_ms - self.preroll_ms and not data['frames']:
                continue
            if data['last_saved_ms'] is not None and ms - data['last_saved_ms'] < 120:
                continue
            if data['last_saved_ms'] is not None and ms - data['last_saved_ms'] > 750:
                self.reason('frame_gap')
            image = load_frame(generation)
            if not image.startswith(b'\xff\xd8') or not image.endswith(b'\xff\xd9'):
                raise ValueError('Bridge returned an invalid JPEG')
            if data['bytes'] + len(image) > self.max_bytes:
                self.finish('capture_size_limit')
                return
            if data['origin_ms'] is None:
                data['origin_ms'] = ms
            relative = 'frames/f%08d.jpg' % generation
            path = Path(data['root']) / relative
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(image)
            data['frames'].append({'id': 'f%08d' % generation, 'file': relative,
                'media_time_ms': ms - data['origin_ms'], 'received_ms': ms,
                'received_unix_ms': frame['received_unix_ms'],
                'sha256': hashlib.sha256(image).hexdigest(), 'sharpness': 0})
            data['bytes'] += len(image)
            data['last_saved_ms'] = ms
        write_json(Path(data['root']) / 'session.json', data)
        if self.max_seconds is not None and now - data['started_monotonic'] >= self.max_seconds:
            self.finish('capture_duration_limit')
        elif data['close_at'] is not None and now - data['close_at'] >= self.tail_ms / 1000:
            self.finish()
            self.armed = True

    def run(self, stopped):
        with httpx.Client(base_url=self.bridge, timeout=2, trust_env=False) as http:
            def get(path):
                response = http.get(path)
                response.raise_for_status()
                return response
            while not stopped.is_set():
                with recognition_lock(self.recognition):
                    disabled = self.recognition and not self.recognition.enabled
                    if disabled:
                        self.pause_recognition()
                if disabled:
                    stopped.wait(.1)
                    continue
                try:
                    self.step(get('/api/doors/state').json(), get('/api/camera/cache').json(),
                              lambda generation: get('/api/camera/frames/%d.jpg' % generation).content)
                except Exception:
                    try:
                        self.finish('capture_transport_or_disk_error')
                    except Exception:
                        self.active = None
                    self.armed = False
                    self.status = {'connected': False, 'state': 'error'}
                stopped.wait(.1)
        self.finish('service_stopped')
