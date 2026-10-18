import json
from pathlib import Path
import queue
import tempfile
import unittest
import wave

from pc_wake import SharedMicrophone, WakeRecorder


BLOCK = b'\x01\x00' * 800


class Doors:
    opened = False
    connected = True

    def snapshot(self):
        return {'connected': self.connected, 'state': {'open': [self.opened, False, False]}}


class Engine:
    trigger = True
    talking = False

    def reset(self):
        pass

    def keyword(self, data):
        result, self.trigger = self.trigger, False
        return result

    def speech(self, data):
        return self.talking


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.doors, self.now = Doors(), [0]
        self.wake = WakeRecorder(self.doors, None, self.temporary.name, clock=lambda: self.now[0])
        self.wake.engine = Engine()

    def feed(self, count=1):
        for _ in range(count):
            self.wake.feed(BLOCK)

    def manifests(self):
        return list(Path(self.temporary.name).glob('*/session.json'))

    def test_wake_speech_silence_creates_one_complete_wav_and_waits_for_backend(self):
        self.feed()
        self.assertEqual(self.wake.snapshot()['status'], 'recording')
        self.wake.engine.talking = True
        self.feed(10)
        self.wake.engine.talking = False
        self.feed(25)
        paths = self.manifests()
        self.assertEqual(len(paths), 1)
        metadata = json.loads(paths[0].read_text())
        self.assertEqual(metadata['origin'], 'wake')
        self.assertTrue(metadata['complete'])
        with wave.open(str(paths[0].parent / 'audio.wav')) as audio:
            self.assertEqual(audio.getframerate(), 16000)
            self.assertEqual(audio.getnframes(), metadata['frames'])
            self.assertEqual(audio.readframes(800), BLOCK)
        self.wake.engine.trigger = True
        self.feed(100)
        self.assertEqual(len(self.manifests()), 1)
        self.assertEqual(self.wake.snapshot()['status'], 'processing')
        self.wake.control({'completed': 'audio-other'})
        self.assertIsNotNone(self.wake.pending)
        self.wake.control({'completed': metadata['session_id']})
        self.feed()
        self.assertEqual(self.wake.snapshot()['event'], 2)

    def test_keyword_without_question_is_not_submitted(self):
        self.feed(102)
        self.assertEqual(self.manifests(), [])
        self.assertIsNone(self.wake.active)

    def test_open_door_cancels_wake_question_and_blocks_keyword(self):
        self.feed()
        self.doors.opened = True
        self.feed(20)
        self.assertIsNone(self.wake.active)
        self.assertEqual(self.wake.snapshot()['status'], 'paused')
        self.assertEqual(self.manifests(), [])
        self.doors.opened = False
        self.wake.engine.trigger = True
        self.feed()
        self.assertEqual(self.wake.snapshot()['status'], 'recording')

    def test_playback_leases_are_per_client_and_expire_if_browser_disappears(self):
        self.wake.control({'client': 'tab1', 'paused': True})
        self.wake.control({'client': 'tab2', 'paused': True})
        self.wake.control({'client': 'tab1', 'paused': False})
        self.feed()
        self.assertEqual(self.wake.snapshot()['event'], 0)
        self.now[0] = 16
        self.feed()
        self.assertEqual(self.wake.snapshot()['event'], 1)

    def test_camera_disconnect_does_not_leave_wake_paused_by_a_stale_open_state(self):
        self.doors.opened = True
        self.doors.connected = False
        self.feed()
        self.assertEqual(self.wake.snapshot()['status'], 'recording')

    def test_disabled_or_lost_samples_discard_incomplete_question(self):
        self.feed()
        self.wake.feed(BLOCK, overflow=True)
        self.assertIsNone(self.wake.active)
        self.assertEqual(self.manifests(), [])
        self.wake.control({'enabled': False})
        self.feed()
        self.assertEqual(self.wake.snapshot()['status'], 'disabled')
        with self.assertRaises(ValueError):
            self.wake.control({'enabled': 'true'})

    def test_continuous_speech_has_bounded_duration(self):
        self.wake.max_seconds = 1
        self.feed()
        self.wake.engine.talking = True
        self.feed(21)
        self.assertEqual(len(self.manifests()), 1)
        self.assertIsNone(self.wake.active)


class SharedMicrophoneTests(unittest.TestCase):
    def test_two_subscribers_share_one_stream_and_closing_one_keeps_the_other(self):
        class Source:
            name = 'test microphone'
            closed = 0

            def __init__(self):
                self.blocks = queue.Queue()

            def read(self):
                value = self.blocks.get(timeout=2)
                if value is None:
                    raise RuntimeError('aborted')
                return value, False

            def close(self):
                self.closed += 1

            def abort(self):
                self.blocks.put(None)

        source, created = Source(), []
        def factory():
            created.append(source)
            return source
        hub = SharedMicrophone(source_factory=factory)
        door, wake = hub.subscribe(), hub.subscribe()
        hub.start()
        try:
            source.blocks.put(BLOCK)
            self.assertEqual(door.read(), (BLOCK, False))
            self.assertEqual(wake.read(), (BLOCK, False))
            door.close()
            source.blocks.put(BLOCK)
            self.assertEqual(wake.read(), (BLOCK, False))
            self.assertEqual(len(created), 1)
            self.assertEqual(source.closed, 0)
        finally:
            wake.close()
            hub.stop()
        self.assertEqual(source.closed, 1)


if __name__ == '__main__':
    unittest.main()
