"""Verify live CanMV frame/event delivery and connection failures."""

import base64
import io
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from pc_canmv import CanMVBridge


class CanMVBridgeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.storage = Path(directory.name)
        client, self.server = socket.socketpair()
        self.addCleanup(self.server.close)
        self.bridge = CanMVBridge(self.storage)
        self.bridge.channel = client
        self.bridge.reader = client.makefile("rb")
        self.addCleanup(self.bridge.__exit__, None, None, None)

    def send(self, *messages):
        self.server.sendall("".join(json.dumps(item) + "\n" for item in messages).encode())

    def test_frames_and_script_events_are_preserved_during_preview_request(self):
        frames = []
        self.bridge.frame_handler = frames.append
        jpeg = b"\xff\xd8camera\xff\xd9"
        self.send(
            {"type": "event", "event": {"event": "scriptOutput", "params": {"text": "event one\n"}}},
            {"type": "frame", "frameId": 1, "dataBase64": ""},
            {"type": "frame", "frameId": 2, "dataBase64": "!bad-base64!"},
            {"type": "frame", "frameId": 3, "dataBase64": base64.b64encode(jpeg).decode(), "chunkTs": 123, "dispatchTs": 124},
            {"type": "event", "event": {"event": "scriptOutput", "params": {"text": "event two\n"}}},
            {"type": "response", "response": {"id": 99, "result": {}}},
            {"type": "response", "response": {"id": 1, "result": {"success": True}}},
        )
        self.assertEqual(self.bridge.request("startPreview"), {"success": True})
        self.assertEqual(list(self.bridge.output), ["event one\n", "event two\n"])
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["data"], jpeg)
        self.assertEqual(frames[0]["frame_id"], 3)
        self.assertEqual(frames[0]["chunk_ts"], 123)
        self.assertEqual(frames[0]["dispatch_ts"], 124)
        with self.server.makefile("rb") as reader:
            self.assertEqual(json.loads(reader.readline())["method"], "startPreview")

    def test_live_preview_does_not_require_sd_card_file_reads(self):
        self.bridge.snapshot = {"board": {"capabilities": {"readFile": False}}}
        self.bridge.require_board()
        self.send({"type": "event", "event": {"event": "mcpBridgeSnapshot", "params": {"board": None}}})
        self.bridge.receive()
        with self.assertRaisesRegex(ConnectionError, "no connected board"):
            self.bridge.require_board()

    def test_disconnected_board_response_keeps_connection_guidance(self):
        self.send({"type": "response", "response": {"id": 1, "error": {"code": 1004}}})
        with self.assertRaisesRegex(ConnectionError, "CanMV: Connect Board"):
            self.bridge.request("startPreview")

    def test_preview_request_failure_is_reported(self):
        self.send({"type": "response", "response": {"id": 1, "error": {"message": "Preview failed"}}})
        with self.assertRaisesRegex(RuntimeError, "startPreview: Preview failed"):
            self.bridge.request("startPreview")

    def test_closed_bridge_and_partial_message_are_rejected(self):
        self.server.sendall(b'{"type":"frame"}')
        self.server.shutdown(socket.SHUT_WR)
        with self.assertRaisesRegex(ValueError, "Invalid or oversized"):
            self.bridge.receive()
        with self.assertRaisesRegex(ConnectionError, "keep VS Code open"):
            self.bridge.receive()

    def test_expired_deadline_does_not_block(self):
        with self.assertRaises(TimeoutError):
            self.bridge.receive(time.monotonic() - 1)

    def test_authentication_failure_closes_transport(self):
        (self.storage / "mcp-bridge-token").write_text("test-token\n", encoding="utf-8")
        channel = Mock()
        channel.makefile.return_value = reader = io.BytesIO(b'{"type":"hello","ok":false}\n')
        with patch("pc_canmv.sys.platform", "darwin"), patch("pc_canmv.socket.socket", return_value=channel):
            with self.assertRaisesRegex(ConnectionError, "authentication failed"):
                with CanMVBridge(self.storage):
                    self.fail("Unauthenticated bridge must not be usable")
        self.assertEqual(json.loads(channel.sendall.call_args.args[0]), {"type": "hello", "token": "test-token"})
        self.assertTrue(reader.closed)
        channel.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
