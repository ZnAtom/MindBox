"""Verify USB event integrity, atomic output, and interleaved IDE messages."""

import base64
from collections import deque
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest

import pc_usb_pull as usb


class FakeBoard:
    def __init__(self, files, read_limit=usb.CHUNK_SIZE):
        self.files = files
        self.read_limit = read_limit
        self.reads = []
        self.stats = {}
        self.acks = []
        self.output = deque()
        self.changed_path = None
        self.corrupt_path = None

    def running(self):
        return True

    def request(self, method, params):
        path = params["path"]
        data = self.files[path]
        if method == "io.queryFileStat":
            self.stats[path] = self.stats.get(path, 0) + 1
            mtime = 2 if self.changed_path == path and self.stats[path] > 1 else 1
            return {"exists": True, "type": "file", "size": len(data), "mtime": mtime}
        if method == "io.readFile":
            self.reads.append(dict(params))
            start = params["offset"]
            block = data[start:start + min(params["size"], self.read_limit)]
            if self.corrupt_path == path and block:
                block = bytes([block[0] ^ 1]) + block[1:]
            return {"dataBase64": base64.b64encode(block).decode("ascii")}
        raise AssertionError(method)

    def write_ack(self, event_id, receipt):
        self.acks.append((event_id, receipt))

    def next_output(self):
        if not self.output:
            raise KeyboardInterrupt
        return self.output.popleft()


def event_for(files):
    return {
        "type": "files_ready", "event_id": "0123456789abcdef",
        "mode": "record_item", "prompt": "Store the item location.",
        "files": [{"path": path, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                  for path, data in files.items()],
    }


class FatJSONBridge(usb.CanMVBridge):
    def __init__(self):
        self.files = {}

    def request(self, method, params):
        if method == "io.writeFile":
            self.files[params["path"]] = base64.b64decode(params["dataBase64"])
            return {"success": True}
        if method == "io.queryFileStat":
            return {"exists": params["path"] in self.files}
        if method == "io.deleteFile":
            existed = params["path"] in self.files
            self.files.pop(params["path"], None)
            return {"success": existed}
        if method == "io.renameFile":
            if params["newPath"] in self.files:
                return {"success": False}
            self.files[params["newPath"]] = self.files.pop(params["oldPath"])
            return {"success": True}
        raise AssertionError(method)


class USBPullTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)
        self.captured = redirect_stdout(io.StringIO())
        self.captured.__enter__()
        self.addCleanup(self.captured.__exit__, None, None, None)

    def test_short_reads_and_multiple_files_keep_bytes_and_prompt(self):
        files = {"/sdcard/event/video.mp4": bytes(range(256)) * 300,
                 "/data/audio.wav": b"audio payload", "/data/empty.bin": b""}
        board = FakeBoard(files, read_limit=5000)
        event = event_for(files)
        receipt = usb.process_event(board, event, self.output)
        destination = self.output / event["event_id"]
        for remote, data in files.items():
            self.assertEqual((destination / Path(remote).name).read_bytes(), data)
        request = json.loads((destination / "request.json").read_text())
        self.assertEqual(request["mode"], event["mode"])
        self.assertEqual(request["prompt"], event["prompt"])
        self.assertTrue(all(item["sha256_verified"] for item in request["files"]))
        self.assertEqual(receipt["bytes"], sum(map(len, files.values())))
        self.assertEqual(board.acks, [(event["event_id"], receipt)])
        self.assertTrue(all(item["size"] <= usb.CHUNK_SIZE for item in board.reads))
        self.assertFalse((self.output / (".receiving-" + event["event_id"])).exists())

    def test_corrupt_second_file_removes_entire_event_and_returns_failure(self):
        files = {"/data/first.wav": b"valid", "/data/second.mp4": b"corrupted in transit"}
        board = FakeBoard(files)
        board.corrupt_path = "/data/second.mp4"
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            usb.process_event(board, event_for(files), self.output)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertEqual(len(board.acks), 1)
        self.assertFalse(board.acks[0][1]["ok"])

    def test_file_changed_during_download_is_rejected(self):
        files = {"/data/test.wav": b"file still being recorded"}
        board = FakeBoard(files)
        board.changed_path = "/data/test.wav"
        with self.assertRaisesRegex(ValueError, "changed during download"):
            usb.process_event(board, event_for(files), self.output)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(board.acks[0][1]["ok"])

    def test_invalid_manifest_is_rejected_before_reading(self):
        files = {"/data/test.wav": b"data"}
        for path in ("/data/../secret", "/etc/passwd", "test.wav", "/data/request.json"):
            with self.subTest(path=path):
                event = event_for(files)
                event["files"][0]["path"] = path
                with self.assertRaises(ValueError):
                    usb.validate_event(event)
        event = event_for(files)
        event["files"].append(dict(event["files"][0], path="/sdcard/test.wav"))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            usb.validate_event(event)
        event = event_for(files)
        del event["files"][0]["sha256"]
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            usb.validate_event(event)

    def test_fragmented_terminal_notification_and_duplicate_ack(self):
        files = {"/data/test.wav": b"one download"}
        board = FakeBoard(files)
        event = event_for(files)
        line = usb.EVENT_PREFIX + json.dumps(event) + "\r\n"
        board.output.extend(["ordinary board output\r\n", line[:23], line[23:], line])
        with self.assertRaises(KeyboardInterrupt):
            usb.watch(board, self.output)
        self.assertEqual(len(board.reads), 1)
        self.assertEqual(len(board.acks), 2)
        self.assertEqual(board.acks[0], board.acks[1])

    def test_ack_replacement_works_with_fat_rename_semantics(self):
        bridge = FatJSONBridge()
        event_id = "0123456789abcdef"
        bridge.write_ack(event_id, {"event_id": event_id, "ok": False})
        receipt = {"event_id": event_id, "ok": True}
        bridge.write_ack(event_id, receipt)
        bridge.write_ack(event_id, receipt)
        self.assertEqual(len(bridge.files), 1)
        self.assertEqual(json.loads(next(iter(bridge.files.values()))), receipt)

    def test_new_control_cannot_overwrite_an_unconsumed_command(self):
        bridge = FatJSONBridge()
        bridge.write_control("open", "record_item", "prompt")
        with self.assertRaises(OSError):
            bridge.write_control("close", "record_item", "prompt")
        command = json.loads(bridge.files["/data/mindbox-control.json"])
        self.assertEqual(command["action"], "open")

    def test_receiver_restart_repeats_ack_without_reading_board_files(self):
        files = {"/data/test.wav": b"persisted download"}
        event = event_for(files)
        first = FakeBoard(files)
        receipt = usb.process_event(first, event, self.output)
        restarted = FakeBoard({})
        self.assertEqual(usb.process_event(restarted, event, self.output), receipt)
        self.assertEqual(restarted.reads, [])
        self.assertEqual(restarted.acks, [(event["event_id"], receipt)])

    def test_corrupted_local_copy_never_gets_a_success_ack_on_retry(self):
        files = {"/data/test.wav": b"persisted download"}
        event = event_for(files)
        usb.process_event(FakeBoard(files), event, self.output)
        (self.output / event["event_id"] / "test.wav").write_bytes(b"different download")
        restarted = FakeBoard({})
        with self.assertRaises(ValueError):
            usb.process_event(restarted, event, self.output)
        self.assertEqual(restarted.acks, [])

    def test_capture_metadata_is_preserved(self):
        files = {"/data/test.wav": b"data"}
        event = event_for(files)
        event["capture"] = {"actual_pre_roll_s": 10.5, "pre_roll_complete": True}
        usb.process_event(FakeBoard(files), event, self.output)
        request = json.loads((self.output / event["event_id"] / "request.json").read_text())
        self.assertEqual(request["capture"], event["capture"])

    def test_notifications_during_request_are_not_lost(self):
        client, server = socket.socketpair()
        self.addCleanup(server.close)
        bridge = usb.CanMVBridge(self.output)
        bridge.channel = client
        bridge.reader = client.makefile("rb")
        self.addCleanup(bridge.__exit__, None, None, None)
        frames = []
        bridge.frame_handler = frames.append
        jpeg = b"\xff\xd8camera\xff\xd9"
        messages = [
            {"type": "event", "event": {"event": "scriptOutput", "params": {"text": "event one\n"}}},
            {"type": "frame", "frameId": 1, "dataBase64": ""},
            {"type": "frame", "frameId": 2, "dataBase64": "!bad-base64!"},
            {"type": "frame", "frameId": 3, "dataBase64": base64.b64encode(jpeg).decode(), "chunkTs": 123, "dispatchTs": 124},
            {"type": "event", "event": {"event": "scriptOutput", "params": {"text": "event two\n"}}},
            {"type": "response", "response": {"id": 1, "result": {"running": True}}},
        ]
        server.sendall("".join(json.dumps(item) + "\n" for item in messages).encode())
        self.assertTrue(bridge.running())
        self.assertEqual(bridge.next_output(), "event one\n")
        self.assertEqual(bridge.next_output(), "event two\n")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0]["data"], jpeg)
        self.assertEqual(frames[0]["frame_id"], 3)
        self.assertEqual(frames[0]["chunk_ts"], 123)


if __name__ == "__main__":
    unittest.main()
