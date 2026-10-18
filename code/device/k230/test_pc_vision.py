"""Exercise real ArUco detection and the single-drawer visibility/audio contract."""

from contextlib import redirect_stdout
from http.client import HTTPConnection
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from pc_audio import DoorAudioRecorder
from pc_door_bridge import CameraFrames, DoorLines, DoorServer, DoorState
from pc_vision import MARKER_IDS, VisionDoors, marker_png, vision_modules

try:
    cv2, np = vision_modules()
except RuntimeError:
    cv2 = np = None


@unittest.skipIf(cv2 is None, "Install requirements-vision.txt for vision tests")
class VisionTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = DoorState(clock=lambda: self.now)
        self.camera = CameraFrames(clock=lambda: self.now)
        self.vision = VisionDoors(self.state, self.camera, clock=lambda: self.now)
        self.markers = {index: self.square(500, 70 + (index - 1) * 200) for index in MARKER_IDS}

    def square(self, x, y, size=80):
        return np.float32([[x, y], [x + size - 1, y], [x + size - 1, y + size - 1], [x, y + size - 1]])

    def scene(self, observations):
        image = np.full((720, 960), 255, np.uint8)
        dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        entries = observations.items() if isinstance(observations, dict) else observations
        for marker_id, corners in entries:
            x, y = corners[0].astype(int)
            size = int(corners[1][0] - corners[0][0] + 1)
            image[y:y + size, x:x + size] = cv2.aruco.generateImageMarker(dictionary, marker_id, size)
        return cv2.imencode(".jpg", image)[1].tobytes()

    def capture(self, observations):
        self.camera.accept({"data": self.scene(observations)})
        self.vision.tick()
        self.now += .11

    def settle(self, observations, frames=5):
        for _ in range(frames):
            self.capture(observations)

    def open_drawer(self, drawer):
        self.settle({drawer: self.markers[drawer]})
        self.assertEqual(self.state.snapshot()["state"]["open"], [index == drawer for index in MARKER_IDS])

    def test_printed_png_round_trips_to_correct_ids_and_no_reference_marker(self):
        for marker_id in MARKER_IDS:
            image = cv2.imdecode(np.frombuffer(marker_png(marker_id), np.uint8), cv2.IMREAD_GRAYSCALE)
            _, ids, _ = self.vision.detector.detectMarkers(image)
            self.assertEqual(ids.flatten().tolist(), [marker_id])
        with self.assertRaises(ValueError):
            marker_png(0)

    def test_startup_needs_fresh_absence_frames_before_reporting_closed(self):
        self.capture({})
        self.assertEqual(self.vision.snapshot()["status"], "initializing")
        self.assertEqual(self.state.snapshot()["state"]["known"], [False] * 3)
        self.settle({}, frames=19)
        snapshot = self.state.snapshot()
        self.assertTrue(snapshot["connected"])
        self.assertEqual(snapshot["state"]["open"], [False] * 3)
        self.assertEqual(snapshot["state"]["known"], [True] * 3)

    def test_each_id_opens_without_geometry_calibration(self):
        for drawer in MARKER_IDS:
            self.open_drawer(drawer)
            self.assertEqual(self.state.snapshot()["state"]["known"], [True] * 3)
            self.assertEqual(self.vision.snapshot()["status"], "tracking")

    def test_opening_needs_duration_and_multiple_observations(self):
        visible = {1: self.markers[1]}
        self.settle(visible, frames=3)
        self.assertEqual(self.state.snapshot()["state"]["open"], [False] * 3)
        self.assertEqual(self.vision.snapshot()["status"], "opening_pending")
        self.capture(visible)
        self.assertTrue(self.state.snapshot()["state"]["open"][0])

    def test_single_flash_or_alternating_ids_do_not_open_any_drawer(self):
        self.capture({1: self.markers[1]})
        self.settle({}, frames=20)
        for drawer in (1, 2) * 5:
            self.capture({drawer: self.markers[drawer]})
        self.assertEqual(self.state.snapshot()["state"]["open"], [False] * 3)

    def test_short_occlusion_holds_open_and_reappearance_cancels_closure(self):
        self.open_drawer(1)
        self.settle({}, frames=10)
        self.assertTrue(self.state.snapshot()["state"]["open"][0])
        self.assertEqual(self.vision.snapshot()["status"], "closing_pending")
        self.capture({1: self.markers[1]})
        self.assertEqual(self.vision.snapshot()["status"], "tracking")
        self.settle({}, frames=10)
        self.assertTrue(self.state.snapshot()["state"]["open"][0])
        self.settle({}, frames=10)
        self.assertEqual(self.state.snapshot()["state"]["open"], [False] * 3)

    def test_stationary_or_moving_visible_marker_stays_open(self):
        self.open_drawer(2)
        self.settle({2: self.square(120, 100, 120)}, frames=25)
        self.assertEqual(self.state.snapshot()["state"]["open"], [False, True, False])

    def test_confirmed_new_id_switches_drawers_without_two_open_states(self):
        self.open_drawer(1)
        self.settle({2: self.markers[2]}, frames=3)
        self.assertEqual(self.state.snapshot()["state"]["open"], [True, False, False])
        self.capture({2: self.markers[2]})
        snapshot = self.state.snapshot()["state"]
        self.assertEqual(snapshot["open"], [False, True, False])
        self.assertEqual(snapshot["kind"], "snapshot")

    def test_multiple_ids_preserve_pose_and_require_confirmation_after_conflict(self):
        self.open_drawer(1)
        self.settle({1: self.markers[1], 3: self.markers[3]}, frames=25)
        self.assertEqual(self.vision.snapshot()["status"], "multiple_markers")
        self.assertEqual(self.state.snapshot()["state"]["open"], [True, False, False])
        self.assertEqual(self.state.snapshot()["state"]["known"], [False] * 3)
        self.capture({1: self.markers[1]})
        self.assertEqual(self.state.snapshot()["state"]["known"], [False] * 3)
        self.open_drawer(1)
        self.assertEqual(self.state.snapshot()["state"]["known"], [True] * 3)

    def test_long_processing_gap_does_not_count_as_continuous_absence(self):
        self.open_drawer(3)
        self.settle({}, frames=8)
        self.now += .7
        self.settle({}, frames=13)
        self.assertTrue(self.state.snapshot()["state"]["open"][2])
        self.assertEqual(self.state.snapshot()["state"]["known"], [False] * 3)
        self.settle({}, frames=7)
        self.assertEqual(self.state.snapshot()["state"]["open"], [False] * 3)

    def test_frozen_frame_and_camera_disconnect_never_count_as_closure(self):
        self.open_drawer(2)
        self.capture({})
        self.now += 1
        self.vision.tick()
        self.assertTrue(self.state.snapshot()["state"]["open"][1])
        self.now += 1
        self.vision.tick()
        self.assertFalse(self.state.snapshot()["connected"])
        self.assertEqual(self.vision.snapshot()["status"], "no_camera")
        self.assertTrue(self.state.snapshot()["state"]["open"][1])
        self.camera.disconnect()
        self.vision.tick()
        self.assertTrue(self.vision.opened[1])

    def test_camera_reconnection_changes_identity_and_reconfirms_current_state(self):
        self.open_drawer(1)
        boot = self.state.snapshot()["state"]["boot_id"]
        self.camera.disconnect()
        self.capture({})
        snapshot = self.state.snapshot()["state"]
        self.assertNotEqual(snapshot["boot_id"], boot)
        self.assertEqual(snapshot["known"], [False] * 3)
        self.assertTrue(snapshot["open"][0])
        self.settle({}, frames=19)
        self.assertEqual(self.state.snapshot()["state"]["open"], [False] * 3)
        self.assertEqual(self.state.snapshot()["state"]["known"], [True] * 3)

    def test_unrelated_or_tiny_markers_do_not_open_a_drawer(self):
        self.settle({0: self.square(80, 60), 4: self.square(200, 60), 1: self.square(500, 70, 20)}, frames=20)
        self.assertEqual(self.state.snapshot()["state"]["open"], [False] * 3)
        self.assertFalse(any(marker["visible"] for marker in self.vision.snapshot()["markers"]))

    def test_duplicate_id_is_an_error_not_a_drawer_transition(self):
        self.camera.accept({"data": self.scene([(1, self.square(80, 60)), (1, self.square(500, 60))])})
        with self.assertRaisesRegex(ValueError, "Duplicate marker"):
            self.vision.tick()
        self.assertIsNone(self.state.snapshot()["state"])

    def test_bad_jpeg_in_worker_marks_state_unavailable_and_holds_pose(self):
        self.open_drawer(1)
        self.camera.accept({"data": b"\xff\xd8bad-jpeg\xff\xd9"})
        self.vision.start()
        try:
            deadline = time.monotonic() + 2
            while self.vision.snapshot()["status"] != "error" and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual(self.vision.snapshot()["status"], "error")
            self.assertFalse(self.state.snapshot()["connected"])
            self.assertTrue(self.vision.opened[0])
        finally:
            self.vision.stop()

    def test_key_heartbeats_cannot_overwrite_vision_state(self):
        self.open_drawer(3)
        board_state = DoorState()
        parser = DoorLines(board_state)
        event = {"schema": 1, "device_id": "k230-key-01", "cabinet_id": "cabinet-01", "boot_id": "0123456789abcdef",
                 "seq": 1, "source": "key", "open": [False] * 3, "kind": "snapshot", "drawer": None}
        parser.feed("MINDBOX_DOOR " + json.dumps(event) + "\n")
        self.assertEqual(self.state.snapshot()["state"]["open"], [False, False, True])
        self.assertEqual(board_state.snapshot()["state"]["open"], [False] * 3)

    def test_audio_waits_for_open_and_survives_occlusion_until_confirmed_close_and_tail(self):
        from test_pc_audio import FakeMicrophone
        recorder = DoorAudioRecorder(self.state, Path(self.directory.name) / "audio", enabled=True,
                                     clock=lambda: self.now, source_factory=FakeMicrophone, tail_seconds=2)
        with redirect_stdout(io.StringIO()):
            self.capture({1: self.markers[1]})
            recorder.step(self.state.snapshot())
            self.assertIsNone(recorder.active)
            self.open_drawer(1)
            recorder.step(self.state.snapshot())
            recorder.read_once()
            self.settle({}, frames=10)
            recorder.step(self.state.snapshot())
            self.assertEqual(recorder.snapshot()["status"], "recording")
            self.settle({}, frames=10)
            recorder.step(self.state.snapshot())
            self.assertEqual(recorder.snapshot()["status"], "tail")
            self.now += 2.01
            recorder.step(self.state.snapshot())
            self.assertIsNone(recorder.active)
            self.assertEqual(recorder.snapshot()["last"]["stop_reason"], "all_closed")
            recorder.stop()

    def test_conflict_keeps_existing_audio_but_does_not_start_new_audio(self):
        from test_pc_audio import FakeMicrophone
        recorder = DoorAudioRecorder(self.state, self.directory.name, enabled=True,
                                     clock=lambda: self.now, source_factory=FakeMicrophone)
        with redirect_stdout(io.StringIO()):
            self.open_drawer(1)
            recorder.step(self.state.snapshot())
            recorder.read_once()
            self.settle({1: self.markers[1], 2: self.markers[2]}, frames=25)
            recorder.step(self.state.snapshot())
            self.assertEqual(recorder.snapshot()["status"], "recording")
            recorder.stop()
            new_recorder = DoorAudioRecorder(self.state, self.directory.name, enabled=True,
                                             source_factory=lambda: self.fail("Conflict cannot open the microphone"))
            new_recorder.step(self.state.snapshot())
            self.assertIsNone(new_recorder.active)

    def test_metadata_separates_camera_and_detection_rates(self):
        visible = {1: self.markers[1]}
        self.capture(visible)
        for _ in range(3):
            self.camera.accept({"data": self.scene(visible)})
            self.now += .03
        self.vision.tick()
        snapshot = json.loads(json.dumps(self.vision.snapshot()))
        self.assertTrue(snapshot["camera_connected"])
        self.assertEqual(snapshot["camera_epoch"], self.camera.snapshot()["epoch"])
        self.assertGreater(snapshot["camera_fps"], snapshot["processing_fps"])
        self.assertGreater(snapshot["processing_fps"], 0)
        self.assertEqual(snapshot["observation_age_ms"], 30)
        self.assertGreater(snapshot["processing_ms"], 0)
        self.assertEqual(len(snapshot["markers"][0]["corners"]), 4)
        self.assertAlmostEqual(snapshot["markers"][0]["corners"][0][0], 500, delta=1)
        self.now += .6
        self.assertGreater(self.vision.snapshot()["observation_age_ms"], 500)
        self.camera.disconnect()
        self.assertFalse(self.vision.snapshot()["camera_connected"])
        self.now += 3
        self.assertEqual(self.vision.snapshot()["processing_fps"], 0)

    def test_invalid_confirmation_times_are_rejected(self):
        for opened, closed in ((0, 2), (.3, .2), (.3, 31), (float("nan"), 2), (.3, float("inf"))):
            with self.assertRaises(ValueError):
                VisionDoors(self.state, self.camera, open_confirm_seconds=opened, close_delay_seconds=closed)

    def test_http_has_visibility_state_three_markers_and_no_calibration(self):
        self.open_drawer(2)
        server = DoorServer(("127.0.0.1", 0), self.state)
        server.camera, server.vision = self.camera, self.vision
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("GET", "/api/vision/state")
            response = connection.getresponse()
            snapshot = json.loads(response.read())
            self.assertEqual(snapshot["mode"], "marker_visibility")
            self.assertEqual([marker["id"] for marker in snapshot["markers"]], [1, 2, 3])
            self.assertNotIn("calibrated", snapshot)
            for marker_id in MARKER_IDS:
                connection.request("GET", "/api/vision/markers/%d.png" % marker_id)
                response = connection.getresponse()
                self.assertEqual(response.getheader("Content-Type"), "image/png")
                self.assertTrue(response.read().startswith(b"\x89PNG"))
            connection.request("GET", "/api/vision/markers/0.png")
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
            connection.request("POST", "/api/vision/calibrate", '{"confirm_closed":true}',
                               {"Content-Type": "application/json"})
            response = connection.getresponse()
            self.assertEqual(response.status, 404)
            response.read()
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
