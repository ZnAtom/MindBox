"""Check multi-click boundaries, explicit door states and SSE recovery."""

from contextlib import redirect_stdout
from http.client import HTTPConnection
import io
import json
import threading
import unittest
from unittest.mock import patch

import k230_key_door as board
from pc_door_bridge import CameraFrames, DoorLines, DoorServer, DoorState


def message(seq=1, doors=None, boot="0123456789abcdef", drawer=None):
    doors = [False, False, False] if doors is None else doors
    return {"schema": 1, "device_id": "k230-key-01", "cabinet_id": "cabinet-01",
            "boot_id": boot, "seq": seq, "source": "key", "open": doors, "drawer": drawer,
            "kind": "snapshot" if drawer is None else "door.opened" if doors[drawer - 1] else "door.closed"}


class ClickTests(unittest.TestCase):
    def trace(self, presses, until=2000, noise=(), clock=lambda value: value):
        detector = board.KeyClicks()
        groups = []
        for now in range(0, until + 1, 5):
            low = any(start <= now < end for start, end in (*presses, *noise))
            count = detector.poll(0 if low else 1, clock(now))
            if count is not None:
                groups.append((now, count))
        return groups

    def test_single_double_triple_resolve_once_after_last_release(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                presses = [(50 + n * 150, 100 + n * 150) for n in range(count)]
                self.assertEqual(self.trace(presses), [(625 + (count - 1) * 150, count)])

    def test_bounce_and_held_second_press_do_not_emit_single(self):
        self.assertEqual(self.trace([(50, 100), (200, 1200)], noise=[(10, 20), (105, 115)]), [(1725, 2)])

    def test_long_hold_is_only_one_click(self):
        self.assertEqual(self.trace([(50, 1300)]), [(1825, 1)])

    def test_late_press_is_a_new_group(self):
        self.assertEqual(self.trace([(50, 100), (700, 750)]), [(625, 1), (1275, 1)])

    def test_four_clicks_are_not_mistaken_for_valid_drawers(self):
        self.assertEqual(self.trace([(50 + n * 150, 100 + n * 150) for n in range(4)]), [(1075, 4)])

    def test_click_group_survives_clock_wrap(self):
        with patch.object(board, "ticks_diff", lambda a, b: (a - b + 1024) % 2048 - 1024):
            self.assertEqual(self.trace([(50, 100), (200, 250)], until=1000, clock=lambda t: (1900 + t) % 2048), [(775, 2)])


class DoorStateTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.state = DoorState(clock=lambda: self.now)

    def test_duplicate_and_out_of_order_packets_do_not_toggle_or_refresh(self):
        event = message(2, [False, True, False], drawer=2)
        self.assertTrue(self.state.accept(event))
        self.assertFalse(self.state.accept(event))
        self.assertFalse(self.state.accept(message(1)))
        self.assertEqual(self.state.snapshot()["state"]["open"], [False, True, False])
        self.now = 8
        self.assertFalse(self.state.snapshot()["connected"])
        self.assertTrue(self.state.accept(message(3, [False, True, False])))
        self.assertTrue(self.state.snapshot()["connected"])

    def test_new_boot_resets_state_and_old_boot_cannot_overwrite_it(self):
        old = message(50, [True, True, True])
        self.state.accept(old)
        self.state.accept(message(1, boot="aaaaaaaaaaaaaaaa"))
        self.assertFalse(self.state.accept(dict(old, seq=51)))
        self.assertEqual(self.state.snapshot()["state"]["open"], [False, False, False])

    def test_malformed_state_and_inconsistent_transition_are_rejected(self):
        for changes in ({"open": [1, False, False]}, {"seq": True}, {"drawer": True, "kind": "door.opened"},
                        {"cabinet_id": "other"}, {"kind": "door.opened", "drawer": 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.state.accept(dict(message(), **changes))

    def test_disconnect_keeps_last_known_state(self):
        self.state.accept(message(1, [True, False, False]))
        self.state.disconnect()
        self.assertFalse(self.state.snapshot()["connected"])
        self.assertTrue(self.state.snapshot()["state"]["open"][0])

    def test_fragmented_output_and_bad_line_do_not_lose_later_event(self):
        parser = DoorLines(self.state)
        line = board.EVENT_PREFIX + json.dumps(message(1, [False, False, True], drawer=3)) + "\r\n"
        with redirect_stdout(io.StringIO()):
            parser.feed("ordinary log\n" + board.EVENT_PREFIX + "{bad json}\n" + line[:17])
            parser.feed(line[17:])
        self.assertEqual(self.state.snapshot()["state"]["open"], [False, False, True])


class CameraCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.camera = CameraFrames(clock=lambda: self.now, cache_seconds=10)

    def frame(self, frame_id):
        return {"frame_id": frame_id, "data": b"\xff\xd8frame-%d\xff\xd9" % frame_id}

    def test_keeps_recent_frames_and_prunes_old_arrivals(self):
        self.assertTrue(self.camera.accept(self.frame(1)))
        self.now = 5
        self.assertTrue(self.camera.accept(self.frame(2)))
        self.now = 10.01
        self.assertTrue(self.camera.accept(self.frame(3)))
        snapshot = self.camera.snapshot()
        self.assertEqual(snapshot["cached_frames"], 2)
        self.assertEqual(snapshot["latest"]["frame_id"], 3)
        self.assertTrue(snapshot["connected"])

    def test_rejects_oversized_or_non_jpeg_and_marks_transport_stale(self):
        self.assertFalse(self.camera.accept({"frame_id": 1, "data": b"not-jpeg"}))
        self.assertFalse(self.camera.accept({"frame_id": 2, "data": b"\xff\xd8" + b"x" * (8 * 1024 * 1024)}))
        self.assertEqual(self.camera.snapshot()["dropped"], 2)
        self.camera.accept(self.frame(3))
        self.now = 8
        self.assertFalse(self.camera.snapshot()["connected"])

    def test_byte_and_frame_limits_evict_oldest_without_stalling_latest(self):
        for limits in ({"max_bytes": 22}, {"max_frames": 2}):
            with self.subTest(limits=limits):
                camera = CameraFrames(clock=lambda: self.now, **limits)
                for number in (1, 2, 3):
                    camera.accept(self.frame(number))
                self.assertEqual(camera.snapshot()["cached_frames"], 2)
                self.assertLessEqual(camera.snapshot()["cache_bytes"], camera.max_bytes)
                self.assertIsNone(camera.cached_frame(1))
                self.assertEqual(camera.latest_jpeg()["frame_id"], 3)

    def test_expiry_and_disconnect_invalidate_frame_references(self):
        self.camera.accept(self.frame(1))
        epoch = self.camera.cache_snapshot()["epoch"]
        self.now = 11
        self.assertEqual(self.camera.cache_snapshot()["frames"], [])
        self.assertIsNone(self.camera.cached_frame(1))
        self.assertIsNone(self.camera.latest_jpeg())
        self.camera.accept(self.frame(2))
        self.camera.disconnect()
        self.assertNotEqual(self.camera.snapshot()["epoch"], epoch)
        self.assertIsNone(self.camera.cached_frame(2))
        self.assertIsNone(self.camera.latest_jpeg())
        self.assertFalse(self.camera.snapshot()["connected"])

    def test_new_board_boot_clears_previous_camera_epoch(self):
        state = DoorState()
        parser = DoorLines(state, on_restart=self.camera.disconnect)
        parser.feed(board.EVENT_PREFIX + json.dumps(message()) + "\n")
        self.camera.accept(self.frame(1))
        parser.feed(board.EVENT_PREFIX + json.dumps(message(boot="aaaaaaaaaaaaaaaa")) + "\n")
        self.assertEqual(self.camera.cache_snapshot()["frames"], [])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = DoorServer(("127.0.0.1", 0), DoorState(), simulate=True)
        self.server.simulate_door()
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)

    def connection(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        self.addCleanup(connection.close)
        return connection

    def event(self, response):
        for _ in range(12):
            line = response.fp.readline()
            if line.startswith(b"data: "):
                return json.loads(line[6:])
        self.fail("No SSE door snapshot")

    def test_live_sse_change_and_reconnect_receive_full_current_state(self):
        stream = self.connection()
        stream.request("GET", "/api/doors/events")
        response = stream.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        self.assertEqual(self.event(response)["state"]["open"], [False, False, False])
        self.server.simulate_door(3)
        self.assertEqual(self.event(response)["state"]["open"], [False, False, True])
        response.close()
        again = self.connection()
        again.request("GET", "/api/doors/events")
        replay = again.getresponse()
        self.assertEqual(self.event(replay)["state"]["open"], [False, False, True])
        replay.close()

    def test_simulation_is_explicit_and_can_be_disabled(self):
        connection = self.connection()
        connection.request("POST", "/api/doors/simulate", json.dumps({"drawer": 2}), {"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        data = json.loads(response.read())
        self.assertEqual(data["state"]["source"], "simulation")
        self.assertEqual(data["state"]["open"], [False, True, False])
        self.server.simulate = False
        connection.request("POST", "/api/doors/simulate", '{"drawer":1}')
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        response.read()

    def test_cache_metadata_and_exact_frame_download_expire_together(self):
        now = [0.0]
        self.server.camera = CameraFrames(clock=lambda: now[0])
        jpeg = b"\xff\xd8example\xff\xd9"
        self.server.camera.accept({"data": jpeg, "frame_id": 42})
        connection = self.connection()
        connection.request("GET", "/api/camera/cache")
        metadata = json.loads(connection.getresponse().read())
        self.assertEqual(metadata["timestamp_basis"], "host_monotonic_arrival")
        self.assertEqual(metadata["frames"][0]["frame_id"], 42)
        connection.request("GET", "/api/camera/frames/1.jpg?retry=1")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), jpeg)
        now[0] = 11
        connection.request("GET", "/api/camera/frames/1.jpg")
        response = connection.getresponse()
        self.assertEqual(response.status, 404)
        response.read()

    def test_mjpeg_sends_new_frames_and_closes_on_disconnect(self):
        jpeg = b"\xff\xd8first\xff\xd9"
        self.server.camera.accept({"data": jpeg, "frame_id": 1})
        connection = self.connection()
        connection.request("GET", "/api/camera/mjpeg")
        response = connection.getresponse()
        self.assertIn("multipart/x-mixed-replace", response.getheader("Content-Type"))
        self.assertEqual(response.fp.readline(), b"--frame\r\n")
        self.assertEqual(response.fp.readline(), b"Content-Type: image/jpeg\r\n")
        self.assertEqual(response.fp.readline(), b"Content-Length: 9\r\n")
        self.assertEqual(response.fp.readline(), b"\r\n")
        self.assertEqual(response.fp.read(len(jpeg) + 2), jpeg + b"\r\n")
        self.server.camera.accept({"data": b"\xff\xd8second\xff\xd9", "frame_id": 2})
        self.assertEqual(response.fp.readline(), b"--frame\r\n")
        self.server.camera.disconnect()
        response.read()


if __name__ == "__main__":
    unittest.main()
