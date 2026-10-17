"""Record the PC microphone during door sessions, independently of USB I/O."""

import json
from pathlib import Path
import sys
import threading
import time
import uuid
import wave


SAMPLE_RATE = 16000
BLOCK_FRAMES = 800
MAX_SESSION_SECONDS = 3600


def sounddevice_module():
    try:
        import sounddevice
    except ImportError as error:
        raise RuntimeError("Install requirements-audio.txt using the Python that runs this bridge") from error
    return sounddevice


def input_devices():
    sd = sounddevice_module()
    default = sd.default.device[0]
    return [{"id": index, "name": item["name"], "default": index == default,
             "channels": item["max_input_channels"]}
            for index, item in enumerate(sd.query_devices()) if item["max_input_channels"]]


class Microphone:
    def __init__(self, device=None):
        sd = sounddevice_module()
        self.name = sd.query_devices(device, "input")["name"]
        self.stream = sd.RawInputStream(device=device, samplerate=SAMPLE_RATE,
                                       channels=1, dtype="int16", latency="low")
        try:
            self.stream.start()
        except BaseException:
            self.stream.close()
            raise

    def read(self):
        data, overflow = self.stream.read(BLOCK_FRAMES)
        data = bytes(data)
        if sys.byteorder != "little":
            from array import array
            samples = array("h", data)
            samples.byteswap()
            data = samples.tobytes()
        return data, overflow

    def close(self):
        try:
            self.stream.stop()
        finally:
            self.stream.close()

    def abort(self):
        self.stream.abort()


class DoorAudioRecorder:
    def __init__(self, state, output, enabled=False, device=None, tail_seconds=2,
                 clock=time.monotonic, source_factory=None, max_seconds=MAX_SESSION_SECONDS):
        if not 0 <= tail_seconds <= 30 or max_seconds <= 0:
            raise ValueError("Invalid audio session duration")
        self.state, self.output, self.enabled = state, Path(output).resolve(), enabled
        self.tail_seconds, self.clock, self.max_seconds = tail_seconds, clock, max_seconds
        self.source_factory = source_factory or (lambda: Microphone(device))
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None
        self.active = None
        self.close_at = None
        self.blocked = False
        self.identity = None
        self.public = {"enabled": enabled, "status": "idle" if enabled else "disabled",
                       "session": None, "last": None, "error": None}

    def snapshot(self):
        with self.lock:
            return dict(self.public)

    def _publish(self, **values):
        with self.lock:
            self.public.update(values)

    def start(self):
        if self.enabled and self.thread is None:
            self.thread = threading.Thread(target=self.run, name="pc-microphone", daemon=True)
            self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread is None:
            if self.active:
                self._finish("shutdown")
            return
        self.thread.join(timeout=2)
        if self.thread.is_alive() and self.active:
            self.active["source"].abort()
            self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError("Microphone worker did not stop")

    def run(self):
        try:
            while not self.stopped.is_set():
                try:
                    self.step(self.state.snapshot())
                    if self.active:
                        self.read_once()
                    else:
                        self.stopped.wait(.05)
                except Exception as error:
                    if self.active:
                        self._finish("capture_error", str(error))
                    self.blocked = True
                    self._publish(status="error", error=str(error))
                    print("PC audio failed:", error, flush=True)
                    self.stopped.wait(.1)
        finally:
            if self.active:
                self._finish("shutdown")

    def step(self, snapshot):
        """Apply a door snapshot; only the recorder worker touches the device."""
        if not self.enabled:
            return
        message = snapshot.get("state")
        connected = snapshot.get("connected") and message is not None
        identity = (message["device_id"], message["boot_id"]) if message else None
        if identity != self.identity:
            if self.active:
                self._finish("device_restarted")
            self.identity, self.blocked = identity, False
        if not connected:
            if self.active:
                self._finish("device_disconnected")
            return
        now = self.clock()
        opened = any(message["open"])
        known = message.get("known", [True, True, True])
        observed_open = any(value and known[index] for index, value in enumerate(message["open"]))
        uncertain = not all(known)
        if not opened and not uncertain:
            self.blocked = False
        if observed_open and not self.active and not self.blocked:
            try:
                self._begin(message, snapshot.get("received_monotonic", now))
            except Exception as error:
                self.blocked = True
                self._publish(status="error", session=None, error=str(error))
                print("PC microphone unavailable:", error, flush=True)
                return
        if not self.active:
            return
        metadata = self.active["metadata"]
        if (message["open"] != metadata["door_events"][-1]["open"]
                or list(known) != metadata["door_events"][-1].get("known", [True, True, True])):
            metadata["door_events"].append(self._event(message, snapshot.get("received_monotonic", now)))
        if opened or uncertain:
            self.close_at = None
            self._publish(status="recording")
        else:
            if self.close_at is None:
                self.close_at = now + self.tail_seconds
                self._publish(status="tail")
            if now >= self.close_at:
                self._finish("all_closed")
                return
        if now - metadata["stream_started_monotonic"] >= self.max_seconds:
            self._finish("duration_limit")
            self.blocked = True

    @staticmethod
    def _event(message, received_at):
        return {"seq": message["seq"], "open": list(message["open"]),
                "known": list(message.get("known", [True, True, True])),
                "kind": message["kind"], "received_monotonic": received_at}

    def _manifest(self, session):
        target = session["directory"] / "session.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(session["metadata"], ensure_ascii=True, indent=2) + "\n")
        temporary.replace(target)

    def _begin(self, message, received_at):
        identity = "audio-" + uuid.uuid4().hex
        directory = self.output / identity
        directory.mkdir(parents=True, exist_ok=False)
        source = writer = None
        try:
            self._publish(status="starting", error=None,
                          session={"id": identity, "input_device": None, "duration_seconds": 0})
            writer = wave.open(str(directory / "audio.part.wav"), "wb")
            writer.setparams((1, 2, SAMPLE_RATE, 0, "NONE", "not compressed"))
            source = self.source_factory()
            now = self.clock()
            metadata = {
                "schema": 1, "session_id": identity, "status": "recording",
                "cabinet_id": message["cabinet_id"], "device_id": message["device_id"],
                "boot_id": message["boot_id"], "source": "pc_microphone",
                "input_device": source.name, "sample_rate": SAMPLE_RATE,
                "channels": 1, "sample_width_bytes": 2, "audio_pre_roll_seconds": 0,
                "tail_seconds": self.tail_seconds, "trigger_received_monotonic": received_at,
                "stream_started_monotonic": now, "stream_started_unix_ms": round(time.time() * 1000),
                "trigger_to_stream_ms": round((now - received_at) * 1000),
                "timestamp_basis": "host_monotonic_stream_start_estimate",
                "door_events": [self._event(message, received_at)],
                "frames": 0, "overflow_blocks": 0,
            }
            self.active = {"source": source, "writer": writer, "metadata": metadata, "directory": directory}
            self.close_at = None
            self._manifest(self.active)
            self._publish(status="recording", error=None,
                          session={"id": identity, "input_device": source.name, "duration_seconds": 0})
            print("PC audio recording:", directory, flush=True)
        except Exception as error:
            self.active = None
            try:
                if source is not None:
                    source.close()
            finally:
                if writer is not None:
                    writer.close()
            (directory / "session.json").write_text(json.dumps({"schema": 1, "session_id": identity,
                "status": "error", "complete": False, "error": str(error)}, indent=2) + "\n")
            raise

    def read_once(self):
        session = self.active
        data, overflow = session["source"].read()
        if not data or len(data) % 2:
            raise ValueError("Microphone returned an invalid PCM block")
        session["writer"].writeframes(data)
        metadata = session["metadata"]
        metadata["frames"] += len(data) // 2
        metadata["overflow_blocks"] += int(bool(overflow))
        self._publish(session={"id": metadata["session_id"], "input_device": metadata["input_device"],
                               "duration_seconds": round(metadata["frames"] / SAMPLE_RATE, 3)})

    def _finish(self, reason, error=None):
        session, self.active = self.active, None
        self.close_at = None
        if session is None:
            return
        try:
            session["source"].close()
        except Exception as close_error:
            error = error or str(close_error)
        try:
            session["writer"].close()
            (session["directory"] / "audio.part.wav").replace(session["directory"] / "audio.wav")
        except Exception as write_error:
            error = error or str(write_error)
        metadata = session["metadata"]
        complete = reason == "all_closed" and not error and metadata["frames"] > 0 and not metadata["overflow_blocks"]
        metadata.update(status="completed" if complete else "incomplete", complete=complete,
                        stop_reason=reason, error=error, duration_seconds=round(metadata["frames"] / SAMPLE_RATE, 3),
                        stopped_monotonic=self.clock(),
                        audio_file="audio.wav" if (session["directory"] / "audio.wav").exists() else "audio.part.wav")
        try:
            self._manifest(session)
        except Exception as metadata_error:
            error = error or str(metadata_error)
            complete = False
            print("PC audio metadata could not be saved:", metadata_error, flush=True)
        self._publish(status="error" if error else "idle", session=None, error=error,
                      last={"id": metadata["session_id"], "directory": str(session["directory"]),
                            "complete": complete, "stop_reason": reason,
                            "duration_seconds": metadata["duration_seconds"]})
        print("PC audio saved:", session["directory"], reason, flush=True)
