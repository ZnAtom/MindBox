"""Relay K230 door states and camera frames, with a bounded PC rolling cache."""

import argparse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import signal
import threading
import time
import uuid
from urllib.parse import urlsplit

from pc_usb_pull import CanMVBridge, default_storage
from pc_audio import DoorAudioRecorder, input_devices, sounddevice_module


EVENT_PREFIX = "MINDBOX_DOOR "
HEARTBEAT_TIMEOUT = 7
CAMERA_CACHE_SECONDS = 10
CAMERA_FRAME_TIMEOUT = 7
CAMERA_MAX_FRAME_BYTES = 8 * 1024 * 1024
CAMERA_CACHE_MAX_BYTES = 64 * 1024 * 1024
CAMERA_CACHE_MAX_FRAMES = 1200


class DoorState:
    def __init__(self, cabinet_id="cabinet-01", clock=time.monotonic):
        self.cabinet_id, self.clock = cabinet_id, clock
        self.condition = threading.Condition()
        self.message = None
        self.last_seen = None
        self.retired_boots = set()
        self.transport = False

    def accept(self, message):
        if not isinstance(message, dict) or message.get("schema") != 1 or message.get("cabinet_id") != self.cabinet_id:
            raise ValueError("Unexpected door schema or cabinet")
        if not isinstance(message.get("device_id"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", message["device_id"]):
            raise ValueError("Invalid device identity")
        if not isinstance(message.get("boot_id"), str) or not re.fullmatch(r"[0-9a-f]{16}", message["boot_id"]):
            raise ValueError("Invalid boot identity")
        if type(message.get("seq")) is not int or message["seq"] < 1:
            raise ValueError("Invalid door sequence")
        doors = message.get("open")
        if not isinstance(doors, list) or len(doors) != 3 or any(type(value) is not bool for value in doors):
            raise ValueError("Expected three explicit door states")
        if message.get("source") not in ("key", "simulation", "sensor", "vision"):
            raise ValueError("Unsupported door source")
        if message.get("source") == "vision":
            known = message.get("known")
            if not isinstance(known, list) or len(known) != 3 or any(type(value) is not bool for value in known):
                raise ValueError("Expected three vision observation states")
        kind, drawer = message.get("kind"), message.get("drawer")
        if kind == "snapshot":
            if drawer is not None:
                raise ValueError("A snapshot has no changed drawer")
        elif kind in ("door.opened", "door.closed"):
            if type(drawer) is not int or not 1 <= drawer <= 3 or doors[drawer - 1] != (kind == "door.opened"):
                raise ValueError("Door transition contradicts its state")
        else:
            raise ValueError("Unsupported door event")
        with self.condition:
            boot = (message["device_id"], message["boot_id"])
            if boot in self.retired_boots:
                return False
            if self.message:
                previous = (self.message["device_id"], self.message["boot_id"])
                if boot == previous and message["seq"] <= self.message["seq"]:
                    return False
                if boot != previous:
                    if len(self.retired_boots) >= 64:
                        self.retired_boots.clear()
                    self.retired_boots.add(previous)
            self.message = dict(message, open=list(doors))
            if message.get("source") == "vision":
                self.message["known"] = list(message["known"])
            self.last_seen, self.transport = self.clock(), True
            self.condition.notify_all()
        return True

    def disconnect(self):
        with self.condition:
            self.transport = False
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            connected = self.transport and self.last_seen is not None and self.clock() - self.last_seen < HEARTBEAT_TIMEOUT
            return {"schema": 1, "cabinet_id": self.cabinet_id, "connected": connected,
                    "received_monotonic": self.last_seen,
                    "state": self.message}

    def wait_snapshot(self, previous):
        with self.condition:
            if self.snapshot() == previous:
                self.condition.wait(timeout=1)
            return self.snapshot()


class DoorLines:
    def __init__(self, state, on_restart=None):
        self.state, self.buffer = state, ""
        self.on_restart = on_restart

    def feed(self, text):
        self.buffer += text
        if len(self.buffer) > 65536:
            self.buffer = ""
            raise ValueError("Oversized board output line")
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            if not line.startswith(EVENT_PREFIX):
                continue
            try:
                message = json.loads(line[len(EVENT_PREFIX):])
                previous = self.state.snapshot()["state"]
                if self.state.accept(message):
                    if previous and (previous["device_id"], previous["boot_id"]) != (message["device_id"], message["boot_id"]):
                        if self.on_restart is not None:
                            self.on_restart()
                    if message["kind"] != "snapshot":
                        print("Door:", message["drawer"], message["kind"], "seq", message["seq"], flush=True)
            except (ValueError, TypeError) as error:
                print("Ignored door event:", error, flush=True)


class CameraFrames:
    """Keep a bounded arrival-time cache and expose the newest JPEG frame."""

    def __init__(self, clock=time.monotonic, cache_seconds=CAMERA_CACHE_SECONDS,
                 max_bytes=CAMERA_CACHE_MAX_BYTES, max_frames=CAMERA_CACHE_MAX_FRAMES):
        self.clock = clock
        self.cache_seconds = cache_seconds
        self.condition = threading.Condition()
        self.frames = deque()
        self.latest = None
        self.generation = 0
        self.last_seen = None
        self.transport = False
        self.received = 0
        self.dropped = 0
        self.bytes = 0
        self.max_bytes, self.max_frames = max_bytes, max_frames
        self.epoch = uuid.uuid4().hex[:16]
        self.started_at = self.clock()

    def accept(self, frame):
        data = frame.get("data") if isinstance(frame, dict) else None
        if not isinstance(data, (bytes, bytearray)) or not data:
            return False
        data = bytes(data)
        if len(data) > min(CAMERA_MAX_FRAME_BYTES, self.max_bytes) or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
            self.dropped += 1
            return False
        now = self.clock()
        with self.condition:
            self.generation += 1
            item = {
                "generation": self.generation,
                "epoch": self.epoch,
                "frame_id": frame.get("frame_id"),
                "data": data,
                "received_at": now,
                "chunk_ts": frame.get("chunk_ts"),
                "dispatch_ts": frame.get("dispatch_ts"),
                "received_unix_ms": round(time.time() * 1000),
            }
            self.frames.append(item)
            self.bytes += len(data)
            self.latest = item
            self.last_seen = now
            self.transport = True
            self.received += 1
            self._prune(now)
            self.condition.notify_all()
        return True

    def _prune(self, now):
        cutoff = now - self.cache_seconds
        while self.frames and (self.frames[0]["received_at"] < cutoff or self.bytes > self.max_bytes
                               or len(self.frames) > self.max_frames):
            self.bytes -= len(self.frames.popleft()["data"])

    def disconnect(self):
        with self.condition:
            self.transport = False
            self.frames.clear()
            self.bytes = 0
            self.latest = None
            self.epoch = uuid.uuid4().hex[:16]
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            now = self.clock()
            self._prune(now)
            connected = self.transport and self.last_seen is not None and now - self.last_seen < CAMERA_FRAME_TIMEOUT
            latest = None
            if self.latest:
                latest = {
                    "generation": self.latest["generation"],
                    "frame_id": self.latest["frame_id"],
                    "bytes": len(self.latest["data"]),
                    "age_ms": max(0, round((now - self.latest["received_at"]) * 1000)),
                }
            return {
                "connected": connected,
                "cache_seconds": self.cache_seconds,
                "cached_frames": len(self.frames),
                "cache_bytes": self.bytes,
                "cache_max_bytes": self.max_bytes,
                "buffered_seconds": round(self.frames[-1]["received_at"] - self.frames[0]["received_at"], 3) if self.frames else 0,
                "epoch": self.epoch,
                "timestamp_basis": "host_monotonic_arrival",
                "received_fps": round((len(self.frames) - 1) / max(.001, self.frames[-1]["received_at"] - self.frames[0]["received_at"]), 1) if len(self.frames) > 1 else 0,
                "received": self.received,
                "dropped": self.dropped,
                "latest": latest,
            }

    def latest_jpeg(self):
        with self.condition:
            now = self.clock()
            self._prune(now)
            return self.latest if self.transport and self.last_seen is not None and now - self.last_seen < CAMERA_FRAME_TIMEOUT else None

    def cache_snapshot(self):
        with self.condition:
            self._prune(self.clock())
            return {"epoch": self.epoch, "timestamp_basis": "host_monotonic_arrival",
                    "frames": [{"generation": item["generation"], "frame_id": item["frame_id"],
                                "received_ms": round((item["received_at"] - self.started_at) * 1000),
                                "received_unix_ms": item["received_unix_ms"],
                                "bytes": len(item["data"])} for item in self.frames]}

    def cached_frame(self, generation):
        with self.condition:
            self._prune(self.clock())
            return next((item for item in self.frames if item["generation"] == generation), None)

    def wait_next(self, generation):
        with self.condition:
            current = self.latest["generation"] if self.latest else None
            if current == generation:
                self.condition.wait(timeout=1)
            return self.latest_jpeg()


class DoorServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, state, simulate=False, audio=None, wake=None):
        super().__init__(address, DoorHandler)
        self.state, self.simulate = state, simulate
        self.camera = CameraFrames()
        self.audio = audio
        self.wake = wake
        self.vision = None
        self.simulation_lock = threading.Lock()
        self.simulation_boot = uuid.uuid4().hex[:16]
        self.simulation_seq = 0

    def simulate_door(self, drawer=None):
        with self.simulation_lock:
            previous = self.state.snapshot()["state"]
            doors = list(previous["open"]) if previous else [False, False, False]
            if drawer is not None:
                doors[drawer - 1] = not doors[drawer - 1]
            self.simulation_seq += 1
            self.state.accept({"schema": 1, "device_id": "k230-simulated", "cabinet_id": self.state.cabinet_id,
                "boot_id": self.simulation_boot, "seq": self.simulation_seq, "source": "simulation",
                "kind": "snapshot" if drawer is None else "door.opened" if doors[drawer - 1] else "door.closed",
                "drawer": drawer, "open": doors})


class DoorHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def json_response(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.path = urlsplit(self.path).path
        if self.path == "/api/doors/state":
            self.json_response(200, self.server.state.snapshot())
        elif self.path == "/api/camera/state":
            self.json_response(200, self.server.camera.snapshot())
        elif self.path == "/api/audio/state":
            self.json_response(200, self.server.audio.snapshot() if self.server.audio else
                               {"enabled": False, "status": "disabled", "session": None, "last": None, "error": None})
        elif self.path == "/api/audio/wake/state":
            self.json_response(200, self.server.wake.snapshot() if self.server.wake else
                               {"enabled": False, "status": "disabled", "word": None, "event": 0})
        elif self.path == "/api/audio/sessions":
            self.json_response(200, {"sessions": self.server.audio.completed_sessions() if self.server.audio else []})
        elif re.fullmatch(r"/api/audio/sessions/audio-[a-zA-Z0-9]+/audio.wav", self.path):
            path = self.server.audio.audio_file(self.path.split('/')[4]) if self.server.audio else None
            if path is None:
                self.json_response(404, {"error": "Audio not found"})
            else:
                payload = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        elif self.path == "/api/vision/state":
            self.json_response(200, self.server.vision.snapshot() if self.server.vision else
                               {"enabled": False, "status": "disabled"})
        elif self.path == "/api/vision/preview.jpg":
            payload = self.server.vision.preview_jpeg() if self.server.vision else None
            self.jpeg_response({"data": payload} if payload else None)
        elif re.fullmatch(r"/api/vision/markers/[1-3]\.png", self.path):
            if not self.server.vision:
                self.json_response(409, {"error": "Start the bridge with --door-source vision"})
                return
            from pc_vision import marker_png
            payload = marker_png(int(self.path.rsplit("/", 1)[1][0]))
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/api/camera/cache":
            self.json_response(200, self.server.camera.cache_snapshot())
        elif re.fullmatch(r"/api/camera/frames/[0-9]+\.jpg", self.path):
            frame = self.server.camera.cached_frame(int(self.path.rsplit("/", 1)[1][:-4]))
            self.jpeg_response(frame)
        elif self.path == "/api/camera/latest.jpg":
            frame = self.server.camera.latest_jpeg()
            self.jpeg_response(frame)
        elif self.path == "/api/camera/mjpeg":
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.connection.settimeout(5)
            generation = None
            try:
                while True:
                    frame = self.server.camera.wait_next(generation)
                    if not frame or frame["generation"] == generation:
                        if not self.server.camera.snapshot()["connected"]:
                            return
                        continue
                    generation = frame["generation"]
                    payload = frame["data"]
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload + b"\r\n"
                    )
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        elif self.path == "/api/doors/events":
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.connection.settimeout(5)
            previous = None
            try:
                self.wfile.write(b"retry: 1500\n\n")
                while True:
                    value = self.server.state.snapshot() if previous is None else self.server.state.wait_snapshot(previous)
                    snapshot = json.dumps(value, separators=(",", ":"))
                    if value != previous:
                        self.wfile.write(("event: doors\ndata: " + snapshot + "\n\n").encode())
                        previous = value
                    else:
                        self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
        else:
            self.json_response(404, {"error": "Not found"})

    def do_POST(self):
        self.close_connection = True
        if self.path == '/api/audio/wake/control' and self.server.wake:
            try:
                origin = self.headers.get('Origin')
                if origin and urlsplit(origin).hostname not in ('localhost', '127.0.0.1', '::1'):
                    self.json_response(403, {'error': 'Local origin required'})
                    return
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024:
                    raise ValueError('Invalid request size')
                self.connection.settimeout(3)
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError('Expected an object')
                self.json_response(200, self.server.wake.control(value))
            except (ValueError, AttributeError, OSError) as error:
                self.json_response(400, {'error': str(error)})
            return
        if self.path != "/api/doors/simulate" or not self.server.simulate:
            self.json_response(404, {"error": "Simulation is disabled"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1024:
                raise ValueError("Invalid request size")
            self.connection.settimeout(3)
            drawer = json.loads(self.rfile.read(length)).get("drawer")
            if type(drawer) is not int or not 1 <= drawer <= 3:
                raise ValueError("Expected drawer 1, 2 or 3")
            self.server.simulate_door(drawer)
            self.json_response(200, self.server.state.snapshot())
        except (ValueError, AttributeError, OSError) as error:
            self.json_response(400, {"error": str(error)})

    def jpeg_response(self, frame):
        if frame is None:
            self.json_response(404, {"error": "Camera frame expired or unavailable"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(frame["data"])))
        self.end_headers()
        self.wfile.write(frame["data"])


def watch_usb(state, camera, storage, stopped):
    previous_error = None
    while not stopped.is_set():
        try:
            with CanMVBridge(storage) as bridge:
                bridge.require_board()
                bridge.frame_handler = camera.accept
                print("CanMV connected; waiting for IO21 door events", flush=True)
                try:
                    preview = bridge.request("startPreview")
                    print("Camera preview requested:", preview, flush=True)
                except (OSError, RuntimeError, TimeoutError) as error:
                    print("Camera preview unavailable:", error, flush=True)
                previous_error = None
                lines = DoorLines(state, on_restart=camera.disconnect)
                while not stopped.is_set():
                    if bridge.output:
                        lines.feed(bridge.output.popleft())
                    else:
                        bridge.receive()
                        if not bridge.snapshot.get("board"):
                            raise ConnectionError("K230 disconnected")
        except (OSError, RuntimeError, ValueError) as error:
            state.disconnect()
            camera.disconnect()
            if str(error) != previous_error:
                print("USB waiting:", error, flush=True)
                previous_error = str(error)
            stopped.wait(2)


def main():
    def stop_service(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_service)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--storage", default=str(default_storage()))
    parser.add_argument("--cabinet", default="cabinet-01")
    parser.add_argument("--simulate", action="store_true", help="No USB; enable local simulated door events")
    parser.add_argument("--door-source", choices=("key", "vision"), default="key")
    parser.add_argument("--vision-open-confirm", type=float, default=.3, help="Seconds of marker visibility to confirm opening")
    parser.add_argument("--vision-close-delay", type=float, default=2, help="Seconds of marker absence to confirm closure")
    parser.add_argument("--record-audio", action="store_true", help="Record the PC microphone while doors are open")
    parser.add_argument("--audio-device", help="Input device ID or name; default is the system input")
    parser.add_argument("--audio-output", default=str(Path(__file__).resolve().parent / "recordings"))
    parser.add_argument("--audio-tail", type=float, default=0,
                        help="Seconds after close confirmation before stopping audio (default: 0)")
    parser.add_argument("--list-audio-devices", action="store_true", help="List PC microphones and exit")
    parser.add_argument('--wake-word', help='Enable local Chinese wake detection with this phrase')
    parser.add_argument('--wake-model-dir', help='Directory prepared by download_wake_models.py')
    args = parser.parse_args()
    if args.simulate and args.door_source == "vision":
        parser.error("--simulate cannot be combined with --door-source vision")
    if args.list_audio_devices:
        print(json.dumps(input_devices(), ensure_ascii=False, indent=2))
        return
    if args.record_audio or args.wake_word:
        sounddevice_module()
    device = int(args.audio_device) if args.audio_device and args.audio_device.isdecimal() else args.audio_device
    state, stopped = DoorState(args.cabinet), threading.Event()
    hub = wake = None
    if args.wake_word:
        from pc_wake import SharedMicrophone, WakeRecorder
        hub = SharedMicrophone(device)
        options = {'directory': args.wake_model_dir} if args.wake_model_dir else {}
        wake = WakeRecorder(state, hub, args.audio_output, word=args.wake_word, cabinet=args.cabinet, **options)
    audio = DoorAudioRecorder(state, args.audio_output, enabled=args.record_audio,
                              device=device, tail_seconds=args.audio_tail,
                              source_factory=hub.subscribe if hub else None)
    server = DoorServer(("127.0.0.1", args.port), state, args.simulate, audio=audio, wake=wake)
    if args.door_source == "vision":
        from pc_vision import VisionDoors
        server.vision = VisionDoors(state, server.camera, open_confirm_seconds=args.vision_open_confirm,
                                   close_delay_seconds=args.vision_close_delay)
        server.vision.start()
    audio.start()
    if hub:
        hub.start()
        wake.start()
    if args.simulate:
        def heartbeat():
            while not stopped.is_set():
                server.simulate_door()
                stopped.wait(2)
        worker = threading.Thread(target=heartbeat, daemon=True)
    else:
        board_state = DoorState(args.cabinet) if server.vision else state
        worker = threading.Thread(target=watch_usb, args=(board_state, server.camera, args.storage, stopped), daemon=True)
    worker.start()
    print("Door bridge:", "http://127.0.0.1:%d" % server.server_port,
          "(SIMULATION)" if args.simulate else "(USB)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        try:
            try:
                if server.vision:
                    server.vision.stop()
            finally:
                audio.stop()
        finally:
            try:
                if wake:
                    wake.stop()
                if hub:
                    hub.stop()
            finally:
                server.server_close()


if __name__ == "__main__":
    main()
