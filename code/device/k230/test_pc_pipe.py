"""Exercise Windows pipe framing, deadlines, and VS Code endpoint discovery."""

import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import uuid

from pc_pipe import NamedPipeChannel
from pc_usb_pull import CanMVBridge, bridge_endpoint, default_storage


@unittest.skipUnless(sys.platform == "win32", "Windows named pipes")
class WindowsPipeTests(unittest.TestCase):
    def setUp(self):
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "CreateNamedPipeW": ([wintypes.LPCWSTR] + [wintypes.DWORD] * 6 + [ctypes.c_void_p], wintypes.HANDLE),
            "ConnectNamedPipe": ([wintypes.HANDLE, ctypes.c_void_p], wintypes.BOOL),
            "ReadFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                          ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
            "WriteFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
            "FlushFileBuffers": ([wintypes.HANDLE], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(self.api, name)
            function.argtypes, function.restype = arguments, result

    def server(self, callback):
        endpoint = "\\\\.\\pipe\\mindbox-test-" + uuid.uuid4().hex
        handle = self.api.CreateNamedPipeW(endpoint, 3, 0, 1, 65536, 65536, 0, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        errors = []

        def serve():
            try:
                if not self.api.ConnectNamedPipe(handle, None):
                    error = ctypes.get_last_error()
                    if error != 535:  # Client connected before ConnectNamedPipe.
                        raise ctypes.WinError(error)
                callback(handle)
                self.api.FlushFileBuffers(handle)
            except BaseException as error:
                errors.append(error)
            finally:
                self.api.CloseHandle(handle)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()

        def finish():
            worker.join(3)
            self.assertFalse(worker.is_alive(), "Pipe server did not finish")
            if errors:
                raise errors[0]

        self.addCleanup(finish)
        return endpoint

    def write(self, handle, payload):
        buffer = ctypes.create_string_buffer(payload)
        count = wintypes.DWORD()
        if not self.api.WriteFile(handle, buffer, len(payload), ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        self.assertEqual(count.value, len(payload))

    def read_line(self, handle):
        payload = bytearray()
        while not payload.endswith(b"\n"):
            buffer, count = ctypes.create_string_buffer(4096), wintypes.DWORD()
            if not self.api.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
                raise ctypes.WinError(ctypes.get_last_error())
            self.assertGreater(count.value, 0)
            payload.extend(buffer.raw[:count.value])
        return json.loads(payload)

    def test_windows_storage_and_uri_drive_hash(self):
        with patch.dict(os.environ, {"APPDATA": "C:\\Users\\Test\\AppData\\Roaming"}):
            storage = default_storage()
        self.assertEqual(storage, Path("C:[local path hidden]"))
        identity = hashlib.sha256(("c" + str(storage)[1:]).encode()).hexdigest()[:20]
        self.assertEqual(bridge_endpoint(storage), "\\\\.\\pipe\\canmv-mcp-" + identity)

    def test_authenticated_bridge_handles_fragmented_large_frames_and_events(self):
        jpeg = b"\xff\xd8" + b"image" * 40000 + b"\xff\xd9"

        def respond(handle):
            self.assertEqual(self.read_line(handle), {"type": "hello", "token": "test-token"})
            self.write(handle, b'{"type":"hello","ok":true,"snapshot":{"board":{"capabilities":{"readFile":true}}}}\n')
            request = self.read_line(handle)
            self.assertEqual(request["method"], "scriptRunning")
            messages = [
                {"type": "event", "event": {"event": "scriptOutput", "params": {"text": "heartbeat\n"}}},
                {"type": "frame", "frameId": 42, "dataBase64": base64.b64encode(jpeg).decode()},
                {"type": "response", "response": {"id": request["id"], "result": {"running": True}}},
            ]
            payload = "".join(json.dumps(message) + "\n" for message in messages).encode()
            self.write(handle, payload[:17])
            self.write(handle, payload[17:])

        endpoint = self.server(respond)
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            (storage / "mcp-bridge-token").write_text("test-token", encoding="utf-8")
            with CanMVBridge(storage, endpoint=endpoint) as bridge:
                bridge.require_board()
                frames = []
                bridge.frame_handler = frames.append
                self.assertTrue(bridge.running())
                self.assertEqual(bridge.next_output(), "heartbeat\n")
                self.assertEqual(frames[0]["data"], jpeg)
                self.assertEqual(frames[0]["frame_id"], 42)

    def test_timeout_cancels_read_and_later_read_can_succeed(self):
        release = threading.Event()

        def respond(handle):
            if not release.wait(3):
                raise TimeoutError("Test did not release pipe server")
            self.write(handle, b"recovered\n")

        endpoint = self.server(respond)
        self.addCleanup(release.set)
        channel = NamedPipeChannel()
        self.addCleanup(channel.close)
        channel.settimeout(1)
        channel.connect(endpoint)
        reader = channel.makefile("rb")
        self.addCleanup(reader.close)
        channel.settimeout(.05)
        with self.assertRaises(TimeoutError):
            reader.readline()
        channel.settimeout(1)
        release.set()
        self.assertEqual(reader.readline(), b"recovered\n")

    def test_server_disconnect_returns_eof_and_close_is_idempotent(self):
        endpoint = self.server(lambda handle: None)
        channel = NamedPipeChannel()
        self.addCleanup(channel.close)
        channel.settimeout(1)
        channel.connect(endpoint)
        with channel.makefile("rb") as reader:
            self.assertEqual(reader.readline(), b"")
        channel.close()
        channel.close()


if __name__ == "__main__":
    unittest.main()
