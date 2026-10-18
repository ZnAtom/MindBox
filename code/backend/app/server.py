"""Local inventory API, durable capture and an independent serial analysis worker."""
import argparse
from contextlib import asynccontextmanager, contextmanager
import json
import os
from pathlib import Path
import threading
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import Field, StrictBool
import uvicorn

from .automatic import AutomaticRecorder, inside_path, read_json
from .capture import BridgeCapture, Sessions
from .config import ROOT, Settings
from .database import Conflict, DatabaseError, NotFound
from .inventory_schemas import Strict, ItemValues
from .store import Store
from .voice_api import VoiceAssistant, mount_routes
from .voice_runtime import VoiceRuntime
from .recognition import RecognitionControl


@contextmanager
def service_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.write(b'0')
        stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


class EditItem(Strict):
    expected_version: int = Field(ge=1)
    base_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)
    patch: dict


class AddItem(Strict):
    values: ItemValues
    base_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=200)


class RecognitionMode(Strict):
    enabled: StrictBool


def create_app(database=None, session_root=None, bridge=None, *, workers=True):
    Settings.load()
    database = Path(database or os.getenv('MIND_BOX_DATABASE', ROOT / 'var/mind-box.sqlite3')).resolve()
    store = Store(database)
    store.initialize()
    sessions = Sessions(store, session_root or database.parent / (database.stem + '-sessions'))
    recognition = RecognitionControl()
    capture = BridgeCapture(sessions, bridge or os.getenv('MIND_BOX_BRIDGE_URL', 'http://127.0.0.1:8765'),
                            recognition=recognition)
    recorder = AutomaticRecorder(sessions, recognition=recognition)
    assistant = VoiceAssistant(database=database)
    voice = VoiceRuntime(assistant, sessions, capture.bridge, recognition=recognition)
    stopped = threading.Event()

    @asynccontextmanager
    async def lifespan(_app):
        with service_lock(database.with_suffix('.service.lock')):
            sessions.recover()
            threads = [threading.Thread(target=target, args=(stopped,), daemon=True)
                       for target in (capture.run, recorder.run, voice.run, assistant.tasks.run)] if workers else []
            for thread in threads:
                thread.start()
            try:
                yield
            finally:
                stopped.set()
                # Model requests have a bounded timeout; shutdown must not leave a writer running.
                for thread in threads:
                    thread.join()

    app = FastAPI(title='Mind Box', lifespan=lifespan)
    app.state.store, app.state.sessions = store, sessions
    app.state.capture, app.state.recorder = capture, recorder
    app.state.recognition = recognition
    app.state.assistant, app.state.voice = assistant, voice
    mount_routes(app, assistant)

    @app.middleware('http')
    async def local_mutations(request: Request, call_next):
        origin = request.headers.get('origin')
        if request.method != 'GET' and origin and urlparse(origin).hostname not in ('127.0.0.1', 'localhost', '::1'):
            return JSONResponse({'detail': 'Local UI origin required'}, status_code=403)
        return await call_next(request)

    @app.exception_handler(DatabaseError)
    async def database_error(_request, exc):
        code = 404 if isinstance(exc, NotFound) else 409 if isinstance(exc, Conflict) else 422
        return JSONResponse({'detail': str(exc)}, status_code=code)

    @app.exception_handler(ValueError)
    async def invalid_value(_request, _exc):
        return JSONResponse({'detail': 'Invalid field value or missing evidence'}, status_code=422)

    def picture(item):
        with store.db.connect() as db:
            crop = db.execute("SELECT o.* FROM observations o JOIN item_observations io ON io.observation_id=o.id "
                "WHERE io.item_id=? AND o.kind='crop' ORDER BY o.rowid DESC LIMIT 1", (item['id'],)).fetchone()
            if not crop:
                return None
            metadata = json.loads(crop['metadata_json'])
            frame = db.execute('SELECT id FROM observations WHERE run_id=? AND local_ref=?',
                               (crop['run_id'], metadata['frame_id'])).fetchone()
        return {'thumbnailUrl': '/api/inventory/evidence/' + crop['id'],
                'originalUrl': '/api/inventory/evidence/' + frame['id'] if frame else None,
                'sourceLabel': '自动记录画面', 'mode': 'contain'}

    @app.get('/api/inventory')
    def inventory(query: str | None = None):
        value = store.get_inventory_snapshot(query=query)
        for item in value['items']:
            item['photo'] = picture(item)
        with store.db.connect() as db:
            changes = db.execute('SELECT c.id,c.action,c.after_json,t.created_at FROM inventory_changes c '
                "JOIN inventory_commits t ON t.id=c.commit_id WHERE t.cabinet_id='cabinet-01' "
                'ORDER BY t.revision DESC,c.change_index DESC LIMIT 20').fetchall()
        value['activity'] = [{**dict(c), 'item': json.loads(c['after_json'])} for c in changes]
        for entry in value['activity']:
            del entry['after_json']
        return value

    @app.get('/api/inventory/evidence/{identity}')
    def evidence(identity: str):
        with store.db.connect() as db:
            row = db.execute('SELECT asset_path FROM observations WHERE id=?', (identity,)).fetchone()
        if not row:
            raise NotFound('Evidence not found')
        return FileResponse(inside_path(store.db.path.parent, row[0]), media_type='image/jpeg')

    @app.patch('/api/inventory/items/{identity}')
    def edit(identity: str, body: EditItem):
        allowed = {'name', 'location_id', 'expires_on', 'notes', 'presence_status'}
        if set(body.patch) - allowed:
            raise DatabaseError('Unsupported editable field')
        return store.correct_item('cabinet-01', identity, body.patch, body.expected_version,
            body.base_revision, body.idempotency_key, 'user:local-ui')

    @app.post('/api/inventory/items')
    def add(body: AddItem):
        values = body.values.model_dump(mode='json', exclude_unset=True)
        return store.create_item('cabinet-01', values, body.base_revision,
                                 body.idempotency_key, 'user:local-ui')

    @app.get('/api/recording/state')
    def recording_state():
        with store.db.connect() as db:
            counts = {row[0]: row[1] for row in db.execute('SELECT state,count(*) FROM automatic_sessions GROUP BY state')}
        configured = bool(os.getenv('MIND_BOX_AUTO_API_KEY'))
        return {'capture': capture.status, 'counts': counts, 'model_configured': configured,
                'recognition': recognition.snapshot(),
                'model': os.getenv('MIND_BOX_AUTO_MODEL', 'gpt-6.1-sol'),
                'sessions': [{k: v for k, v in session.items() if k != 'root'} for session in sessions.list()]}

    @app.post('/api/recording/control')
    def recording_control(body: RecognitionMode):
        with recognition.lock:
            if recognition.enabled != body.enabled:
                # Old queued operations require an explicit retry, not a later enable click.
                sessions.pause_queued()
                if not body.enabled:
                    capture.pause_recognition()
                recognition.set_enabled(body.enabled)
            return recognition.snapshot()

    @app.get('/api/recording/sessions/{identity}')
    def session_detail(identity: str):
        value = sessions.get(identity)
        root = Path(value.pop('root'))
        manifest_path = root / 'session.json'
        manifest = read_json(manifest_path) if manifest_path.exists() else {}
        value['capture_policy'] = manifest.get('capture_policy', {'preroll_ms': 2000, 'tail_ms': 2000})
        with store.db.connect() as db:
            rows = db.execute('SELECT id,state,transcript,result_json FROM voice_interactions WHERE session_id=?', (identity,))
            value['voice'] = [{**{k: row[k] for k in ('id', 'state', 'transcript')},
                               'result': json.loads(row['result_json'])} for row in rows]
        value['frames'] = [{**{k: f[k] for k in ('id', 'media_time_ms')},
                            'url': f'/api/recording/sessions/{identity}/frames/{f["id"]}.jpg'}
                           for f in manifest.get('frames', [])]
        analysis_path = root / 'analysis.json'
        if analysis_path.exists():
            state = read_json(analysis_path)['state']
            value['objects'] = [{k: obj.get(k) for k in ('object_id', 'name', 'description', 'presence')}
                                for obj in state['objects'].values() if obj['points']]
            value['summaries'] = state['summaries']
        return value

    @app.get('/api/recording/sessions/{identity}/frames/{frame_id}.jpg')
    def session_frame(identity: str, frame_id: str):
        root = Path(sessions.get(identity)['root'])
        if not (root / 'session.json').is_file():
            raise NotFound('Session archive not available')
        manifest = read_json(root / 'session.json')
        frame = next((f for f in manifest['frames'] if f['id'] == frame_id), None)
        if not frame:
            raise NotFound('Frame not found')
        return FileResponse(inside_path(root, frame['file']), media_type='image/jpeg')

    @app.post('/api/recording/sessions/{identity}/{action}')
    def change_session(identity: str, action: str):
        if action == 'retry' and not recognition.enabled:
            raise Conflict('请先开启物品识别，再重试。')
        with store.db.transaction() as db:
            row = db.execute('SELECT state,root FROM automatic_sessions WHERE id=?', (identity,)).fetchone()
            if not row:
                raise NotFound('Session not found')
            if row['state'] not in ('failed', 'pending'):
                raise Conflict('Only failed or pending sessions can be reviewed')
            if action == 'retry':
                manifest_path = Path(row['root']) / 'session.json'
                if not manifest_path.is_file() or not read_json(manifest_path).get('complete'):
                    raise Conflict('画面不完整，请核对库存后忽略本次记录。')
                state = 'queued'
            elif action == 'dismiss':
                state = 'dismissed'
            else:
                raise HTTPException(404, 'Unknown action')
            db.execute('UPDATE automatic_sessions SET state=? WHERE id=?', (state, identity))
        return {'id': identity, 'state': state}

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8787)
    parser.add_argument('--database')
    parser.add_argument('--sessions')
    parser.add_argument('--bridge')
    parser.add_argument('--no-workers', action='store_true', help='Serve an isolated API fixture without capture/model workers')
    args = parser.parse_args()
    uvicorn.run(create_app(args.database, args.sessions, args.bridge, workers=not args.no_workers), host='127.0.0.1', port=args.port)


if __name__ == '__main__':
    main()
