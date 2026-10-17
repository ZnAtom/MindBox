"""Check door-driven PC recording without using a real microphone."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
import wave

from pc_audio import BLOCK_FRAMES, DoorAudioRecorder, SAMPLE_RATE


class FakeMicrophone:
    name = "Synthetic test input"

    def __init__(self):
        self.closed = False
        self.overflow = False

    def read(self):
        return b"\xe8\x03" * BLOCK_FRAMES, self.overflow

    def close(self):
        self.closed = True


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.sources = []
        self.recorder = DoorAudioRecorder(None, self.temporary.name, enabled=True,
            clock=lambda: self.now, source_factory=self.source)
        self.addCleanup(self.recorder.stop)
        output = redirect_stdout(io.StringIO())
        output.__enter__()
        self.addCleanup(output.__exit__, None, None, None)

    def source(self):
        microphone = FakeMicrophone()
        self.sources.append(microphone)
        return microphone

    def observe(self, doors, seq=1, boot="0123456789abcdef", connected=True):
        self.recorder.step({"connected": connected, "received_monotonic": self.now,
            "state": {"device_id": "k230-key-01", "cabinet_id": "cabinet-01",
                      "boot_id": boot, "seq": seq, "kind": "snapshot", "open": list(doors)}})

    def metadata(self):
        return json.loads((Path(self.recorder.snapshot()["last"]["directory"]) / "session.json").read_text())

    def test_closed_and_disabled_do_not_open_microphone(self):
        self.observe([False, False, False])
        self.assertEqual(self.sources, [])
        self.recorder.enabled = False
        self.observe([True, False, False])
        self.assertEqual(self.sources, [])

    def test_multi_drawer_session_stops_only_after_last_close_and_tail(self):
        self.observe([True, False, False])
        self.recorder.read_once()
        self.observe([True, True, False], seq=2)
        self.observe([False, True, False], seq=3)
        self.assertEqual(len(self.sources), 1)
        self.assertEqual(self.recorder.snapshot()["status"], "recording")
        self.observe([False, False, False], seq=4)
        self.assertEqual(self.recorder.snapshot()["status"], "tail")
        self.now += 1.99
        self.observe([False, False, False], seq=5)
        self.assertFalse(self.sources[0].closed)
        self.recorder.read_once()
        self.now += .02
        self.observe([False, False, False], seq=6)
        self.assertTrue(self.sources[0].closed)
        metadata = self.metadata()
        self.assertTrue(metadata["complete"])
        self.assertEqual(metadata["stop_reason"], "all_closed")
        self.assertEqual(metadata["audio_pre_roll_seconds"], 0)
        self.assertEqual(self.recorder.completed_sessions()[0]["session_id"], metadata["session_id"])
        self.assertIsNotNone(self.recorder.audio_file(metadata["session_id"]))
        self.assertIsNone(self.recorder.audio_file('audio-../../outside'))
        self.assertEqual(len(metadata["door_events"]), 4)
        directory = Path(self.recorder.snapshot()["last"]["directory"])
        self.assertFalse((directory / "audio.part.wav").exists())
        with wave.open(str(directory / "audio.wav"), "rb") as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth(), audio.getframerate()), (1, 2, SAMPLE_RATE))
            self.assertEqual(audio.getnframes(), BLOCK_FRAMES * 2)
            self.assertEqual(audio.readframes(audio.getnframes()), b"\xe8\x03" * BLOCK_FRAMES * 2)

    def test_reopen_during_tail_continues_same_recording(self):
        self.observe([True, False, False])
        identity = self.recorder.snapshot()["session"]["id"]
        self.recorder.read_once()
        self.observe([False, False, False], seq=2)
        self.now += 1
        self.observe([False, False, True], seq=3)
        self.now += 3
        self.observe([False, False, True], seq=4)
        self.assertEqual(self.recorder.snapshot()["session"]["id"], identity)
        self.assertFalse(self.sources[0].closed)
        self.assertEqual(self.recorder.snapshot()["status"], "recording")

    def test_next_session_never_overwrites_previous_wav(self):
        self.observe([True, False, False])
        self.recorder.read_once()
        self.observe([False, False, False], seq=2)
        self.now += 3
        self.observe([False, False, False], seq=3)
        first = self.recorder.snapshot()["last"]
        self.observe([False, True, False], seq=4)
        self.assertNotEqual(first["id"], self.recorder.snapshot()["session"]["id"])
        self.assertTrue((Path(first["directory"]) / "audio.wav").is_file())

    def test_disconnect_closes_device_and_marks_clip_incomplete(self):
        self.observe([True, False, False])
        self.recorder.read_once()
        self.observe([True, False, False], connected=False)
        self.assertTrue(self.sources[0].closed)
        self.assertFalse(self.metadata()["complete"])
        self.assertEqual(self.metadata()["stop_reason"], "device_disconnected")
        self.observe([True, False, False], seq=2)
        self.assertEqual(len(self.sources), 2)

    def test_new_boot_does_not_append_to_previous_audio(self):
        self.observe([True, False, False])
        self.recorder.read_once()
        self.observe([False, False, False], boot="aaaaaaaaaaaaaaaa")
        self.assertEqual(self.metadata()["stop_reason"], "device_restarted")
        self.assertFalse(self.metadata()["complete"])
        self.assertTrue(self.sources[0].closed)

    def test_device_failure_is_not_retried_on_every_heartbeat(self):
        calls = []

        def unavailable():
            calls.append(True)
            raise OSError("Microphone permission denied")

        self.recorder.source_factory = unavailable
        self.observe([True, False, False])
        self.observe([True, False, False], seq=2)
        self.assertEqual(calls, [True])
        self.assertEqual(self.recorder.snapshot()["status"], "error")
        self.assertIn("permission denied", self.recorder.snapshot()["error"])
        self.observe([False, False, False], seq=3)
        self.recorder.source_factory = self.source
        self.observe([True, False, False], seq=4)
        self.assertEqual(self.recorder.snapshot()["status"], "recording")

    def test_overflow_never_claims_a_complete_clip(self):
        self.observe([True, False, False])
        self.sources[0].overflow = True
        self.recorder.read_once()
        self.observe([False, False, False], seq=2)
        self.now += 3
        self.observe([False, False, False], seq=3)
        self.assertEqual(self.metadata()["overflow_blocks"], 1)
        self.assertFalse(self.metadata()["complete"])

    def test_duration_limit_releases_device_until_all_doors_close(self):
        self.recorder.max_seconds = 1
        self.observe([True, False, False])
        self.recorder.read_once()
        self.now += 2
        self.observe([True, False, False], seq=2)
        self.assertTrue(self.sources[0].closed)
        self.assertEqual(self.metadata()["stop_reason"], "duration_limit")
        self.observe([True, False, False], seq=3)
        self.assertEqual(len(self.sources), 1)

    def test_service_shutdown_finalizes_partial_wav(self):
        self.observe([True, False, False])
        self.recorder.read_once()
        self.recorder.stop()
        self.assertTrue(self.sources[0].closed)
        self.assertEqual(self.metadata()["stop_reason"], "shutdown")
        self.assertFalse(self.metadata()["complete"])


if __name__ == "__main__":
    unittest.main()
