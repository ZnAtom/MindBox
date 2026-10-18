"""Local Chinese keyword spotting and command recording on a shared PC microphone."""

from collections import deque
import json
from pathlib import Path
import queue
import threading
import time
import uuid
import wave

from pc_audio import Microphone, SAMPLE_RATE
from download_wake_models import DEFAULT_DIRECTORY, FILES


class MicrophoneTap:
    def __init__(self, hub):
        self.hub, self.blocks = hub, queue.Queue(maxsize=40)
        self.closed = False
        self.overflow = False

    @property
    def name(self):
        return self.hub.name

    def read(self):
        value = self.blocks.get(timeout=2)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RuntimeError('Microphone subscription closed')
        data, overflow = value
        dropped, self.overflow = self.overflow, False
        return data, overflow or dropped

    def close(self):
        with self.hub.lock:
            self.closed = True
            self.hub.taps.discard(self)
        self.push(None)

    abort = close

    def push(self, value):
        try:
            self.blocks.put_nowait(value)
        except queue.Full:
            try:
                self.blocks.get_nowait()
            except queue.Empty:
                pass
            self.overflow = True
            self.blocks.put_nowait(value)


class SharedMicrophone:
    """One physical stream; door and wake workers consume independent bounded queues."""

    def __init__(self, device=None, source_factory=None):
        self.factory = source_factory or (lambda: Microphone(device))
        self.name = 'PC microphone'
        self.lock, self.stopped = threading.Lock(), threading.Event()
        self.taps, self.source, self.thread = set(), None, None

    def subscribe(self):
        tap = MicrophoneTap(self)
        with self.lock:
            self.taps.add(tap)
        return tap

    def start(self):
        self.thread = threading.Thread(target=self.run, name='shared-microphone', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.source:
            self.source.abort()
        if self.thread:
            self.thread.join(timeout=3)
        with self.lock:
            for tap in list(self.taps):
                tap.push(None)

    def run(self):
        try:
            while not self.stopped.is_set():
                with self.lock:
                    subscribers = bool(self.taps)
                if not subscribers:
                    if self.source:
                        self.source.close()
                        self.source = None
                    self.stopped.wait(.05)
                    continue
                try:
                    if self.source is None:
                        self.source = self.factory()
                        self.name = self.source.name
                    value = self.source.read()
                except Exception as error:
                    value = error
                    if self.source:
                        self.source.close()
                        self.source = None
                with self.lock:
                    for tap in self.taps:
                        tap.push(value)
                if isinstance(value, Exception):
                    self.stopped.wait(1)
        finally:
            if self.source:
                self.source.close()
                self.source = None


class WakeEngine:
    def __init__(self, directory, word):
        import numpy as np
        import sherpa_onnx as sherpa
        directory = Path(directory)
        for name in (*FILES, 'silero_vad.onnx'):
            if not (directory / name).is_file():
                raise RuntimeError('Wake models missing; run download_wake_models.py first')
        tokens = sherpa.text2token([word], tokens=str(directory / 'tokens.txt'),
            tokens_type='phone+ppinyin', lexicon=str(directory / 'en.phone'))
        if len(tokens) != 1 or not tokens[0]:
            raise ValueError('Wake word cannot be encoded by the Chinese keyword model')
        keywords = directory / 'keywords.txt'
        keywords.write_text(' '.join(tokens[0]) + ' @' + word + '\n', encoding='utf-8')
        self.kws = sherpa.KeywordSpotter(tokens=str(directory / 'tokens.txt'),
            encoder=str(directory / FILES[0]), decoder=str(directory / FILES[1]),
            joiner=str(directory / FILES[2]), keywords_file=str(keywords),
            keywords_threshold=.25, num_trailing_blanks=1, num_threads=2)
        config = sherpa.VadModelConfig()
        config.silero_vad.model = str(directory / 'silero_vad.onnx')
        config.silero_vad.min_silence_duration = .1
        config.silero_vad.min_speech_duration = .1
        config.sample_rate = SAMPLE_RATE
        self.vad = sherpa.VoiceActivityDetector(config, buffer_size_in_seconds=30)
        self.np, self.window = np, config.silero_vad.window_size
        self.reset()

    def reset(self):
        self.stream = self.kws.create_stream()
        self.vad.reset()
        self.buffer = self.np.empty(0, dtype=self.np.float32)

    def samples(self, data):
        return self.np.frombuffer(data, dtype='<i2').astype(self.np.float32) / 32768

    def keyword(self, data):
        self.stream.accept_waveform(SAMPLE_RATE, self.samples(data))
        found = False
        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)
            found = bool(self.kws.get_result(self.stream)) or found
        return found

    def speech(self, data):
        self.buffer = self.np.concatenate((self.buffer, self.samples(data)))
        speech = False
        while len(self.buffer) >= self.window:
            self.vad.accept_waveform(self.buffer[:self.window])
            self.buffer = self.buffer[self.window:]
            speech = self.vad.is_speech_detected() or speech
            while not self.vad.empty():
                self.vad.pop()
        return speech


class WakeRecorder:
    def __init__(self, doors, hub, output, word='\u4f60\u597d\u5c0f\u76d2',
                 directory=DEFAULT_DIRECTORY, enabled=True, engine_factory=WakeEngine,
                 silence_seconds=1.2, wait_seconds=5, max_seconds=25, clock=time.monotonic,
                 cabinet='cabinet-01'):
        self.doors, self.hub, self.output = doors, hub, Path(output)
        self.word, self.directory, self.enabled = word, directory, enabled
        self.cabinet = cabinet
        self.engine_factory, self.clock = engine_factory, clock
        self.silence_seconds, self.wait_seconds, self.max_seconds = silence_seconds, wait_seconds, max_seconds
        self.lock, self.stopped = threading.RLock(), threading.Event()
        self.thread, self.engine, self.tap, self.active = None, None, None, None
        self.leases, self.pending = {}, None
        self.ring, self.ring_bytes = deque(), 0
        self.public = {'enabled': enabled, 'status': 'loading' if enabled else 'disabled',
                       'word': word, 'event': 0, 'error': None, 'last': None}

    def snapshot(self):
        with self.lock:
            return {**self.public, 'pending': self.pending}

    def control(self, value):
        with self.lock:
            if 'enabled' in value:
                if not isinstance(value['enabled'], bool):
                    raise ValueError('enabled must be boolean')
                self.enabled = value['enabled']
                self.public['enabled'] = self.enabled
            if 'client' in value:
                client = value['client']
                if not isinstance(client, str) or not 1 <= len(client) <= 100 or not isinstance(value.get('paused'), bool):
                    raise ValueError('Invalid microphone lease')
                if value['paused']:
                    self.leases[client] = self.clock() + 15
                else:
                    self.leases.pop(client, None)
            if value.get('completed') == self.pending:
                self.pending = None
        return self.snapshot()

    def start(self):
        self.thread = threading.Thread(target=self.run, name='chinese-wake', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.tap:
            self.tap.close()
        if self.thread:
            self.thread.join(timeout=5)

    def _paused(self):
        snapshot = self.doors.snapshot()
        opened = snapshot.get('connected') and any((snapshot.get('state') or {}).get('open', []))
        with self.lock:
            now = self.clock()
            self.leases = {key: deadline for key, deadline in self.leases.items() if deadline > now}
            return opened or bool(self.leases)

    def run(self):
        try:
            while not self.stopped.is_set():
                try:
                    if not self.enabled:
                        self.cancel('disabled')
                        if self.tap:
                            self.tap.close()
                            self.tap = None
                        self.stopped.wait(.1)
                        continue
                    if self.engine is None:
                        self.engine = self.engine_factory(self.directory, self.word)
                    if self.tap is None:
                        self.tap = self.hub.subscribe()
                    data, overflow = self.tap.read()
                    self.feed(data, overflow)
                except Exception as error:
                    if self.stopped.is_set():
                        break
                    self.cancel('error')
                    with self.lock:
                        self.public['error'] = str(error)
                    self.stopped.wait(1)
        finally:
            self.cancel('stopped')
            if self.tap:
                self.tap.close()

    def cancel(self, status):
        if self.active or self.public['status'] != status:
            self.active = None
            self.ring.clear()
            self.ring_bytes = 0
            if self.engine:
                self.engine.reset()
        with self.lock:
            self.public['status'] = status

    def feed(self, data, overflow=False):
        """Process a single PCM block; durations follow sample counts, not CPU speed."""
        if not data or len(data) % 2:
            raise ValueError('Invalid microphone PCM')
        if not self.enabled or self._paused():
            self.cancel('paused' if self.enabled else 'disabled')
            return
        if self.pending:
            self.cancel('processing')
            return
        if overflow:
            self.cancel('listening')
            return
        with self.lock:
            self.public.update(status='recording' if self.active else 'listening', error=None)
        seconds = len(data) / (2 * SAMPLE_RATE)
        if self.active:
            active = self.active
            active['blocks'].append(data)
            active['elapsed'] += seconds
            if self.engine.speech(data):
                active['speech'] += seconds
                active['silence'] = 0
            else:
                active['silence'] += seconds
            if active['speech'] >= .15 and active['silence'] >= self.silence_seconds:
                self.finish()
            elif active['speech'] < .15 and active['elapsed'] >= self.wait_seconds:
                self.cancel('no_speech')
            elif active['elapsed'] >= self.max_seconds:
                self.finish() if active['speech'] >= .15 else self.cancel('no_speech')
            return
        self.ring.append(data)
        self.ring_bytes += len(data)
        while self.ring_bytes > 2 * SAMPLE_RATE * 2 and len(self.ring) > 1:
            self.ring_bytes -= len(self.ring.popleft())
        if self.engine.keyword(data):
            self.active = {'blocks': list(self.ring), 'elapsed': 0, 'speech': 0, 'silence': 0,
                           'start_ms': round(time.time() * 1000 - self.ring_bytes / 32)}
            self.engine.reset()
            with self.lock:
                self.public.update(status='recording', event=self.public['event'] + 1)

    def finish(self):
        active, self.active = self.active, None
        identity = 'audio-' + uuid.uuid4().hex
        directory = self.output / identity
        directory.mkdir(parents=True, exist_ok=False)
        with wave.open(str(directory / 'audio.part.wav'), 'wb') as writer:
            writer.setparams((1, 2, SAMPLE_RATE, 0, 'NONE', 'not compressed'))
            writer.writeframes(b''.join(active['blocks']))
        (directory / 'audio.part.wav').replace(directory / 'audio.wav')
        manifest = {'schema': 1, 'session_id': identity, 'origin': 'wake', 'complete': True,
                    'cabinet_id': self.cabinet, 'audio_file': 'audio.wav', 'wake_word': self.word,
                    'stream_started_unix_ms': active['start_ms'], 'sample_rate': SAMPLE_RATE,
                    'frames': sum(len(block) // 2 for block in active['blocks'])}
        target = directory / 'session.json'
        target.with_suffix('.json.tmp').write_text(json.dumps(manifest, ensure_ascii=True) + '\n')
        target.with_suffix('.json.tmp').replace(target)
        with self.lock:
            self.pending = identity
            self.public.update(status='processing', last=manifest)
        self.ring.clear()
        self.ring_bytes = 0
        self.engine.reset()
