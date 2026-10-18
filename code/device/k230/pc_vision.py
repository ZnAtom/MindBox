"""Detect one open drawer from upward-facing ArUco markers visible only when open."""

import copy
from collections import deque
import math
import threading
import time
import uuid


MARKER_IDS = (1, 2, 3)
MAX_FRAME_AGE = 1.5
MAX_OBSERVATION_GAP = .5


def vision_modules():
    try:
        import cv2
        import numpy as np
    except ImportError as error:
        raise RuntimeError("Install requirements-vision.txt using the Python that runs this bridge") from error
    return cv2, np


def marker_png(marker_id):
    if marker_id not in MARKER_IDS:
        raise ValueError("Unknown marker ID")
    cv2, np = vision_modules()
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    marker = cv2.aruco.generateImageMarker(dictionary, marker_id, 400)
    image = np.full((500, 500), 255, dtype=np.uint8)
    image[50:450, 50:450] = marker
    success, encoded = cv2.imencode(".png", image)
    if not success:
        raise RuntimeError("Could not encode marker")
    return encoded.tobytes()


class VisionDoors:
    def __init__(self, state, camera, clock=time.monotonic,
                 open_confirm_seconds=.3, close_delay_seconds=2):
        self.cv2, self.np = vision_modules()
        if (not math.isfinite(open_confirm_seconds) or not math.isfinite(close_delay_seconds)
                or not 0 < open_confirm_seconds <= 10 or not open_confirm_seconds < close_delay_seconds <= 30):
            raise ValueError("Expected 0 < open confirmation < close delay <= 30 seconds")
        self.state, self.camera, self.clock = state, camera, clock
        self.open_confirm_seconds, self.close_delay_seconds = open_confirm_seconds, close_delay_seconds
        dictionary = self.cv2.aruco.getPredefinedDictionary(self.cv2.aruco.DICT_4X4_50)
        self.detector = self.cv2.aruco.ArucoDetector(dictionary)
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread = None
        self.opened, self.known = [False] * 3, [False] * 3
        self.pending = None
        self.last_identity = None
        self.last_published = None
        self.published_at = float("-inf")
        self.boot, self.seq = uuid.uuid4().hex[:16], 0
        self.observed_at = None
        self.processed_at = deque(maxlen=60)
        self.public = {"enabled": True, "mode": "marker_visibility", "single_drawer": True, "status": "no_camera",
                       "open_confirm_seconds": open_confirm_seconds, "close_delay_seconds": close_delay_seconds,
                       "pending_drawer": None, "pending_remaining_ms": 0,
                       "markers": [], "open": list(self.opened), "known": list(self.known),
                       "image_size": None, "error": None}

    def snapshot(self):
        camera = self.camera.snapshot()
        with self.lock:
            now = self.clock()
            while self.processed_at and now - self.processed_at[0] > 3:
                self.processed_at.popleft()
            result = copy.deepcopy(self.public)
            result.update(camera_connected=camera["connected"] and camera["latest"]["age_ms"] <= MAX_FRAME_AGE * 1000,
                          camera_fps=camera["received_fps"], camera_epoch=camera["epoch"],
                          observation_age_ms=round(max(0, now - self.observed_at) * 1000) if self.observed_at is not None else None,
                          processing_fps=round((len(self.processed_at) - 1) / (self.processed_at[-1] - self.processed_at[0]), 1)
                          if len(self.processed_at) > 1 and self.processed_at[-1] > self.processed_at[0] else 0)
            return result

    def start(self):
        self.thread = threading.Thread(target=self.run, name="drawer-vision", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError("Vision worker did not stop")
        self.state.disconnect()

    def run(self):
        while not self.stopped.is_set():
            try:
                self.tick()
            except Exception as error:
                with self.lock:
                    self._unknown()
                    self.observed_at = None
                    self.public.update(status="error", error=str(error))
                self.state.disconnect()
            self.stopped.wait(.1)

    def tick(self):
        started_at = time.perf_counter()
        frame = self.camera.latest_jpeg()
        now = self.clock()
        if frame is None or now - frame["received_at"] > MAX_FRAME_AGE:
            with self.lock:
                self._unknown()
                self.observed_at = None
                self.public.update(status="no_camera", markers=[])
            self.state.disconnect()
            return
        identity = (frame["epoch"], frame["generation"])
        if identity == self.last_identity:
            return
        with self.lock:
            if self.last_identity and identity[0] != self.last_identity[0]:
                self._unknown()
                self.observed_at = None
                self.boot, self.seq = uuid.uuid4().hex[:16], 0
                self.last_published, self.published_at = None, float("-inf")
            self.last_identity = identity
        image = self.cv2.imdecode(self.np.frombuffer(frame["data"], self.np.uint8), self.cv2.IMREAD_COLOR)
        if image is None:
            raise ValueError("Camera JPEG could not be decoded")
        raw_corners, raw_ids, _ = self.detector.detectMarkers(image)
        observations = {}
        if raw_ids is not None:
            for raw_id, raw_corner in zip(raw_ids.flatten(), raw_corners):
                marker_id = int(raw_id)
                if marker_id in MARKER_IDS:
                    if marker_id in observations:
                        raise ValueError("Duplicate marker ID %d in camera image" % marker_id)
                    observations[marker_id] = raw_corner.reshape(4, 2)
        height, width = image.shape[:2]
        if self.camera.snapshot()["epoch"] != identity[0]:
            self.state.disconnect()
            return
        self.observe(observations, (width, height), frame["generation"], frame["received_at"])
        with self.lock:
            self.processed_at.append(self.clock())
            self.public["processing_ms"] = round((time.perf_counter() - started_at) * 1000, 1)

    def _edge(self, corners):
        points = self.np.asarray(corners, dtype=float)
        return float(self.np.linalg.norm(points - self.np.roll(points, -1, axis=0), axis=1).mean())

    def _unknown(self):
        self.known, self.pending = [False] * 3, None
        self.public.update(known=list(self.known), pending_drawer=None, pending_remaining_ms=0)

    def observe(self, observations, image_size, generation=None, received_at=None):
        """Debounce appearance/disappearance on fresh frames, under a single-drawer constraint."""
        with self.lock:
            now = self.clock()
            observed_at = now if received_at is None else received_at
            if self.observed_at is not None and not 0 < observed_at - self.observed_at <= MAX_OBSERVATION_GAP:
                self._unknown()
            self.observed_at = observed_at
            visible = {key: self.np.asarray(value, dtype=float) for key, value in observations.items()
                       if key in MARKER_IDS and self._edge(value) >= 28}
            self.public.update(image_size=list(image_size), generation=generation, error=None)
            markers = [{"id": index, "visible": index in visible,
                        "corners": visible[index].tolist() if index in visible else None} for index in MARKER_IDS]
            self.public["markers"] = markers
            if len(visible) > 1:
                self._unknown()
                self.public.update(status="multiple_markers")
                self._publish(now)
                return
            drawer = next(iter(visible), 0)
            target = [index == drawer for index in MARKER_IDS]
            if target == self.opened and all(self.known):
                self.pending = None
                self.public.update(status="tracking", pending_drawer=None, pending_remaining_ms=0)
            else:
                if self.pending is None or self.pending[0] != drawer:
                    self.pending = [drawer, observed_at, 1]
                else:
                    self.pending[2] += 1
                delay = self.open_confirm_seconds if drawer else self.close_delay_seconds
                remaining = max(0, delay - (observed_at - self.pending[1]))
                if remaining <= 1e-9 and self.pending[2] >= 3:
                    self.opened, self.known, self.pending = target, [True] * 3, None
                    self.public.update(status="tracking", pending_drawer=None, pending_remaining_ms=0)
                else:
                    status = "opening_pending" if drawer else "closing_pending" if any(self.opened) else "initializing"
                    self.public.update(status=status, pending_drawer=drawer or None,
                                       pending_remaining_ms=round(remaining * 1000))
            self._publish(now)

    def _publish(self, now):
        self.public.update(open=list(self.opened), known=list(self.known))
        signature = (tuple(self.opened), tuple(self.known))
        if signature == self.last_published and now - self.published_at < 1:
            return
        previous = self.last_published[0] if self.last_published else tuple(self.opened)
        changed = [index + 1 for index, value in enumerate(self.opened) if value != previous[index]]
        drawer = changed[0] if len(changed) == 1 else None
        self.seq += 1
        self.state.accept({"schema": 1, "cabinet_id": self.state.cabinet_id, "device_id": "pc-vision-01",
                           "boot_id": self.boot, "seq": self.seq, "source": "vision", "open": list(self.opened),
                           "known": list(self.known), "drawer": drawer,
                           "kind": "snapshot" if drawer is None else "door.opened" if self.opened[drawer - 1] else "door.closed"})
        self.last_published, self.published_at = signature, now
