"""Pull closed files through the existing CanMV VS Code USB connection."""

import argparse
import base64
from collections import deque
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import sys
import tempfile
import time
import uuid


EVENT_PREFIX = "MINDBOX_EVENT "
CHUNK_SIZE = 32 * 1024
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
CONNECTION_HELP = (
    "The CanMV bridge has no connected board. Connect the board in the VS Code "
    "window that owns the bridge. With CanMV 0.9.10, multiple VS Code windows "
    "can share a bridge belonging to a different window; close unused CanMV "
    "windows, reload the remaining window, and use CanMV: Connect Board."
)


def default_storage():
    if sys.platform == "darwin":
        root = Path.home() / "Library/Application Support/Code"
    elif sys.platform.startswith("linux"):
        root = Path.home() / ".config/Code"
    else:
        raise RuntimeError("This prototype supports the macOS/Linux VS Code bridge")
    return root / "User/globalStorage/kendryte747.canmv-vscode"


class CanMVBridge:
    def __init__(self, storage, endpoint=None):
        self.storage = Path(storage)
        identity = hashlib.sha256(str(self.storage).encode()).hexdigest()[:20]
        self.endpoint = endpoint or str(
            Path(tempfile.gettempdir()) / ("canmv-mcp-" + identity + ".sock")
        )
        self.channel = None
        self.reader = None
        self.next_id = 0
        self.output = deque()
        self.frame_handler = None
        self.snapshot = {}

    def __enter__(self):
        token = (self.storage / "mcp-bridge-token").read_text().strip()
        self.channel = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.channel.settimeout(15)
            self.channel.connect(self.endpoint)
            self.reader = self.channel.makefile("rb")
            self.send({"type": "hello", "token": token})
            hello = self.receive(time.monotonic() + 15)
            if hello.get("type") != "hello" or hello.get("ok") is not True:
                raise ConnectionError("CanMV bridge authentication failed")
            self.snapshot = hello.get("snapshot", {})
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.reader is not None:
            self.reader.close()
        if self.channel is not None:
            self.channel.close()

    def require_board(self):
        board = self.snapshot.get("board")
        if not board:
            raise ConnectionError(CONNECTION_HELP)
        if not board.get("capabilities", {}).get("readFile"):
            raise RuntimeError("The connected firmware does not advertise USB file reads")

    def send(self, value):
        self.channel.sendall((json.dumps(value) + "\n").encode("utf-8"))

    def receive(self, deadline=None):
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            raise TimeoutError("CanMV bridge request timed out")
        self.channel.settimeout(remaining)
        raw = self.reader.readline(MAX_MESSAGE_BYTES + 1)
        if not raw:
            raise ConnectionError("CanMV bridge closed; keep VS Code open")
        if len(raw) > MAX_MESSAGE_BYTES or not raw.endswith(b"\n"):
            raise ValueError("Invalid or oversized CanMV bridge message")
        message = json.loads(raw)
        if message.get("type") == "event":
            event = message.get("event", {})
            if event.get("event") == "scriptOutput":
                self.output.append(str(event.get("params", {}).get("text", "")))
            elif event.get("event") == "mcpBridgeSnapshot":
                self.snapshot = event.get("params", {})
        elif message.get("type") == "frame":
            # CanMV 0.9.10 broadcasts preview frames as JPEG bytes on the
            # shared bridge. Keep them separate from terminal output so a
            # slow HTTP client cannot block board event handling.
            encoded = message.get("dataBase64")
            if self.frame_handler is not None and isinstance(encoded, str):
                try:
                    frame = base64.b64decode(encoded, validate=True)
                except (ValueError, TypeError):
                    frame = b""
                if frame and len(frame) <= 8 * 1024 * 1024:
                    self.frame_handler({
                        "frame_id": message.get("frameId"),
                        "data": frame,
                        "chunk_ts": message.get("chunkTs"),
                        "dispatch_ts": message.get("dispatchTs"),
                        "received_at": time.monotonic(),
                    })
        return message

    def request(self, method, params=None, timeout=15):
        self.next_id += 1
        request_id = self.next_id
        self.send({"type": "request", "id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        while True:
            message = self.receive(deadline)
            response = message.get("response", {})
            if message.get("type") != "response" or response.get("id") != request_id:
                continue
            if "error" in response:
                error = response["error"]
                if error.get("code") == 1004:
                    raise ConnectionError(CONNECTION_HELP)
                raise RuntimeError("%s: %s" % (method, error.get("message", "request failed")))
            return response.get("result", {})

    def next_output(self):
        while not self.output:
            self.receive()
        return self.output.popleft()

    def running(self):
        return self.request("scriptRunning").get("running") is True

    def write_ack(self, event_id, receipt):
        target = "/data/mindbox-ack-" + event_id + ".json"
        self.write_json_file(target, receipt, replace_existing=True)

    def write_control(self, action, mode, prompt):
        self.write_json_file("/data/mindbox-control.json", {
            "action": action, "command_id": uuid.uuid4().hex[:16], "mode": mode, "prompt": prompt,
        })

    def write_json_file(self, target, value, replace_existing=False):
        temporary = target + ".tmp"
        payload = (json.dumps(value, ensure_ascii=True) + "\n").encode()
        result = self.request("io.writeFile", {
            "path": temporary, "dataBase64": base64.b64encode(payload).decode("ascii"),
        })
        if result.get("success") is not True:
            raise OSError("The board rejected the USB JSON file")
        if replace_existing:
            # FAT rename cannot replace an existing ACK. Publish only complete JSON.
            result = self.request("io.deleteFile", {"path": target})
            if result.get("success") is not True:
                if self.request("io.queryFileStat", {"path": target}).get("exists"):
                    raise OSError("The board rejected replacing the previous ACK")
        result = self.request("io.renameFile", {"oldPath": temporary, "newPath": target})
        if result.get("success") is not True:
            raise OSError("The board rejected the USB JSON rename")


def validate_path(value):
    if not isinstance(value, str) or "\x00" in value or "\\" in value:
        raise ValueError("Invalid SD-card path")
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or len(path.parts) < 3:
        raise ValueError("Expected an absolute SD-card file path")
    if path.parts[1] not in ("sdcard", "data"):
        raise ValueError("Only /sdcard and /data files can be pulled")
    if path.name in ("request.json", "transfer.json"):
        raise ValueError("Reserved output filename")
    return str(path)


def pull_file(bridge, remote, target, expected_size=None, expected_digest=None):
    remote = validate_path(remote)
    before = bridge.request("io.queryFileStat", {"path": remote})
    if not before.get("exists") or before.get("type") == "directory":
        raise ValueError("Remote file is missing or is a directory: " + remote)
    size = before.get("size")
    if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
        raise ValueError("Invalid remote file size")
    if expected_size is not None and size != expected_size:
        raise ValueError("Remote file size changed before download")
    digest = hashlib.sha256()
    offset = 0
    next_report = 1024 * 1024
    with target.open("xb") as output:
        while offset < size:
            count = min(CHUNK_SIZE, size - offset)
            result = bridge.request("io.readFile", {"path": remote, "offset": offset, "size": count})
            if "dataBase64" in result:
                block = base64.b64decode(result["dataBase64"], validate=True)
            else:
                block = bytes(result.get("data", []))
            if not block or len(block) > count:
                raise ValueError("Incomplete USB read at offset %d" % offset)
            output.write(block)
            digest.update(block)
            offset += len(block)
            if offset >= next_report or offset == size:
                print("USB read:", remote, offset, "/", size, "bytes", flush=True)
                next_report = offset + 1024 * 1024
    after = bridge.request("io.queryFileStat", {"path": remote})
    if not after.get("exists") or after.get("size") != size or before.get("mtime") != after.get("mtime"):
        raise ValueError("Remote file changed during download")
    actual_digest = digest.hexdigest()
    if expected_digest is not None and actual_digest != expected_digest.lower():
        raise ValueError("SHA-256 mismatch: " + remote)
    return {"name": target.name, "path": remote, "size": size, "sha256": actual_digest,
            "sha256_verified": expected_digest is not None}


def validate_event(event, require_digest=True):
    if not isinstance(event, dict) or event.get("type") != "files_ready":
        raise ValueError("Expected a files_ready event")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{16}", event_id):
        raise ValueError("Invalid event_id")
    files = event.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= 8:
        raise ValueError("Expected one to eight closed files")
    names = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Invalid file descriptor")
        path = validate_path(item.get("path"))
        name = PurePosixPath(path).name
        if name in names:
            raise ValueError("Duplicate output filename")
        names.add(name)
        size = item.get("size")
        if size is not None and (type(size) is not int or not 0 <= size <= MAX_FILE_BYTES):
            raise ValueError("Invalid expected file size")
        digest = item.get("sha256")
        if require_digest and (size is None or digest is None):
            raise ValueError("Board notifications must include file size and SHA-256")
        if digest is not None and (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
            raise ValueError("Invalid expected SHA-256")
    return event_id


def existing_receipt(event, destination):
    request = json.loads((destination / "request.json").read_text(encoding="utf-8"))
    receipt = json.loads((destination / "transfer.json").read_text(encoding="utf-8"))
    if (request.get("event_id") != event["event_id"] or receipt.get("event_id") != event["event_id"]
            or receipt.get("ok") is not True or len(request["files"]) != len(event["files"])
            or request.get("mode") != str(event.get("mode", "transfer_test"))
            or request.get("prompt") != str(event.get("prompt", ""))):
        raise ValueError("Existing event metadata differs from the notification")
    for expected, saved in zip(event["files"], request["files"]):
        local = destination / PurePosixPath(expected["path"]).name
        if (saved.get("path") != expected["path"] or saved.get("size") != expected.get("size")
                or saved.get("sha256") != str(expected.get("sha256", "")).lower()
                or local.stat().st_size != expected["size"]):
            raise ValueError("Existing event files differ from the notification")
        digest = hashlib.sha256()
        with local.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != saved["sha256"]:
            raise ValueError("Existing local event file failed SHA-256 verification")
    return receipt


def process_event(bridge, event, output_root, notify_board=True):
    event_id = validate_event(event, require_digest=notify_board)
    output_root.mkdir(parents=True, exist_ok=True)
    destination = output_root / event_id
    if destination.exists():
        receipt = existing_receipt(event, destination)
        if notify_board:
            bridge.write_ack(event_id, receipt)
        print("USB event already verified; ACK repeated:", event_id, flush=True)
        return receipt
    staging = output_root / (".receiving-" + event_id + "-" + uuid.uuid4().hex[:8])
    staging.mkdir()
    started = time.monotonic()
    try:
        running_before = bridge.running()
        files = [pull_file(bridge, item["path"], staging / PurePosixPath(item["path"]).name,
                           item.get("size"), item.get("sha256")) for item in event["files"]]
        running_after = bridge.running()
        elapsed = max(time.monotonic() - started, 0.000001)
        receipt = {"ok": True, "event_id": event_id, "bytes": sum(item["size"] for item in files),
                   "seconds": round(elapsed, 3), "transport": "canmv_usb_pull",
                   "script_running_before": running_before, "script_running_after": running_after}
        receipt["receive_mib_per_second"] = round(receipt["bytes"] / elapsed / (1024 * 1024), 3)
        request = {"event_id": event_id, "mode": str(event.get("mode", "transfer_test")),
                   "prompt": str(event.get("prompt", "")), "files": files, "source": "k230_usb_notify"}
        if "capture" in event:
            request["capture"] = event["capture"]
        for name, value in (("request.json", request), ("transfer.json", receipt)):
            (staging / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staging.rename(destination)
    except BaseException as error:
        shutil.rmtree(staging, ignore_errors=True)
        if notify_board and isinstance(error, Exception):
            try:
                bridge.write_ack(event_id, {"ok": False, "event_id": event_id, "error": str(error)})
            except Exception:
                pass
        raise
    if notify_board:
        bridge.write_ack(event_id, receipt)
    print("USB event complete:", destination, receipt, flush=True)
    return receipt


def watch(bridge, output):
    print("Waiting for MINDBOX_EVENT from the CanMV terminal...", flush=True)
    buffer = ""
    while True:
        buffer += bridge.next_output()
        if len(buffer) > 64 * 1024:
            buffer = ""
            print("Discarded oversized terminal line", flush=True)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if line.startswith("MINDBOX_STATUS "):
                print(line.rstrip("\r"), flush=True)
            if not line.startswith(EVENT_PREFIX):
                continue
            try:
                event = json.loads(line[len(EVENT_PREFIX):])
                process_event(bridge, event, output)
            except (OSError, RuntimeError, ValueError) as error:
                print("USB event failed:", error, flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_true", help="Check the existing IDE connection")
    group.add_argument("--once", metavar="REMOTE_PATH", help="Download a closed file without a board notification")
    group.add_argument("--door", choices=("open", "close"), help="Send a simulated door event over USB")
    group.add_argument("--status", action="store_true", help="Ask the continuous recorder to print its status")
    group.add_argument("--stop", action="store_true", help="Finish the current event and stop continuous capture")
    parser.add_argument("--mode", default="record_item", help="Mode for --door open")
    parser.add_argument("--prompt", default="Record the items placed in or removed from the cabinet and their locations.")
    parser.add_argument("--sha256", help="Expected checksum for --once")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent / "received_usb")
    parser.add_argument("--storage", type=Path, default=default_storage())
    args = parser.parse_args()
    if args.sha256 and not args.once:
        parser.error("--sha256 requires --once")
    if len(args.mode) > 64 or len(args.prompt) > 2048:
        parser.error("Mode or prompt is too long")
    try:
        with CanMVBridge(args.storage) as bridge:
            bridge.require_board()
            if args.check:
                print("CanMV USB connection available; script running:", bridge.running())
                return 0
            if args.door or args.status or args.stop:
                if not bridge.running():
                    raise RuntimeError("Run k230_event_recorder.py on the board before sending controls")
                action = args.door or ("status" if args.status else "stop")
                bridge.write_control(action, args.mode, args.prompt)
                print("USB recorder control sent:", action)
                return 0
            if args.once:
                event = {"type": "files_ready", "event_id": uuid.uuid4().hex[:16],
                         "files": [{"path": args.once, "sha256": args.sha256}],
                         "mode": "transfer_test", "prompt": "Manual USB file download."}
                process_event(bridge, event, args.output.resolve(), notify_board=False)
            else:
                watch(bridge, args.output.resolve())
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print("USB pull unavailable:", error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
