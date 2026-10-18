"""Associate completed PC recordings with video sessions before interpreting speech."""
from datetime import datetime
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

import httpx

from .client import write_json


def strip_wake_word(text, word):
    if not word:
        return text
    punctuation = r'[\s,\uff0c\u3001]*'
    prefix = punctuation.join(re.escape(char) for char in word)
    cleaned = re.sub(r'^\s*' + prefix + r'[\s,\uff0c\u3002.!\uff01?\uff1f\u3001:]*', '', text)
    if cleaned != text:
        return cleaned
    # KWS already confirmed the phrase; Whisper can render its final syllable as a homophone.
    letters = list(re.finditer(r'[\u3400-\u9fffA-Za-z]', text))[:len(word)]
    candidate = ''.join(match[0] for match in letters)
    if (len(word) >= 4 and len(candidate) == len(word) and candidate[:2] == word[:2]
            and sum(a != b for a, b in zip(candidate, word)) <= 1):
        return text[letters[-1].end():].lstrip(' ,\uff0c\u3002.!\uff01?\uff1f\u3001:')
    return text


class VoiceRuntime:
    def __init__(self, assistant, sessions, bridge):
        self.assistant, self.sessions = assistant, sessions
        self.bridge = bridge.rstrip('/')
        self.status = {'connected': False}

    def collect(self, recordings, download):
        for audio in recordings:
            if not audio.get('complete') or audio.get('cabinet_id') != self.assistant.cabinet_id:
                continue
            with self.assistant.store.db.connect() as db:
                if db.execute('SELECT 1 FROM voice_interactions WHERE audio_session_id=?',
                              (audio['session_id'],)).fetchone():
                    continue
            if audio.get('origin') == 'wake':
                root = self.assistant.output / audio['session_id']
                root.mkdir(parents=True, exist_ok=True)
                path = root / 'audio.wav'
                path.write_bytes(download(audio['session_id']))
                write_json(root / 'audio.json', audio)
                self.assistant.begin(audio['cabinet_id'], 'wake', audio_session=audio['session_id'],
                    audio_path=path, state='queued')
                continue
            candidates = []
            for session in self.sessions.list(limit=1000):
                path = Path(session['root']) / 'session.json'
                if not path.exists():
                    continue
                manifest = json.loads(path.read_text())
                trigger = manifest.get('trigger_monotonic')
                if (trigger is not None and manifest.get('boot_id') == audio.get('boot_id')
                        and abs(trigger - audio['trigger_received_monotonic']) < 2):
                    candidates.append((session, manifest))
            if len(candidates) != 1:
                continue
            session, manifest = candidates[0]
            root = Path(session['root'])
            payload = download(audio['session_id'])
            path = root / 'audio.wav'
            path.write_bytes(payload)
            audio = {**audio, 'video_session_id': session['id'],
                'audio_offset_ms': audio['stream_started_unix_ms'] - manifest['frames'][0]['received_unix_ms']}
            write_json(root / 'audio.json', audio)
            self.assistant.begin(audio['cabinet_id'], 'door', session=session['id'],
                audio_session=audio['session_id'], audio_path=path, state='waiting_inventory')

    def process_next(self):
        with self.assistant.store.db.connect() as db:
            row = db.execute('SELECT v.* FROM voice_interactions v LEFT JOIN automatic_sessions s ON s.id=v.session_id '
                "WHERE (v.origin='wake' AND v.state='queued') OR "
                "(v.state='waiting_inventory' AND s.state='committed') ORDER BY v.created_at LIMIT 1").fetchone()
        if not row:
            return False
        identity = row['id']
        self.assistant.save_result(identity, {}, 'processing')
        try:
            self.assistant.configured()
            root = Path(row['audio_path']).parent
            audio = json.loads((root / 'audio.json').read_text())
            transcript_path = root / 'transcript.json'
            if transcript_path.exists():
                transcript = json.loads(transcript_path.read_text())
            else:
                transcript = self.assistant.transcriber.transcribe(Path(row['audio_path']).read_bytes())
                write_json(transcript_path, transcript)
            # Restrict "this item" to identities actually written in this operation.
            receipts = self.sessions.get(row['session_id'])['detail'].get('receipts', []) if row['session_id'] else []
            scope = sorted({item for receipt in receipts for key in ('added_item_ids', 'updated_item_ids')
                            for item in receipt.get(key, [])})
            started = datetime.fromtimestamp(audio['stream_started_unix_ms'] / 1000,
                                            ZoneInfo(self.assistant.tasks.timezone)).isoformat()
            text = transcript.get('text', '')
            if row['origin'] == 'wake':
                text = strip_wake_word(text, audio.get('wake_word', ''))
            result = self.assistant.understand(text, row['cabinet_id'], identity,
                scope=scope if row['origin'] == 'door' else None, audio=Path(row['audio_path']), allow_silence=True,
                clock={'quality': 'trusted', 'capture_start': started})
            write_json(root / 'voice-result.json', result)
        except Exception as error:
            with self.assistant.store.db.connect() as db:
                current = db.execute('SELECT result_json FROM voice_interactions WHERE id=?', (identity,)).fetchone()
            result = json.loads(current[0]) if current else {}
            self.assistant.save_result(identity, {**result, 'error': self.assistant.client.safe(error)}, 'failed')
        return True

    def run(self, stopped):
        # A wake request has no inventory transaction; retry interrupted work after restart.
        with self.assistant.store.db.transaction() as db:
            db.execute("UPDATE voice_interactions SET state='queued' WHERE origin='wake' AND state='processing'")
        with httpx.Client(base_url=self.bridge, timeout=5, trust_env=False) as http:
            def get(path):
                response = http.get(path)
                response.raise_for_status()
                return response
            while not stopped.is_set():
                try:
                    self.collect(get('/api/audio/sessions').json()['sessions'],
                        lambda identity: get('/api/audio/sessions/' + identity + '/audio.wav').content)
                    self.status = {'connected': True}
                except Exception as error:
                    self.status = {'connected': False, 'error': type(error).__name__}
                self.process_next()
                try:
                    pending = get('/api/audio/wake/state').json().get('pending')
                    if pending:
                        with self.assistant.store.db.connect() as db:
                            finished = db.execute("SELECT 1 FROM voice_interactions WHERE audio_session_id=? "
                                "AND origin='wake' AND state IN ('completed','failed')", (pending,)).fetchone()
                        if finished:
                            response = http.post('/api/audio/wake/control', json={'completed': pending})
                            response.raise_for_status()
                except httpx.HTTPError:
                    pass
                stopped.wait(.5)
