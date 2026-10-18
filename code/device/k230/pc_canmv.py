"""Receive live JPEG frames and script events from the CanMV VS Code bridge."""

import base64
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time


MAX_MESSAGE_BYTES = 16 * 1024 * 1024
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
    elif sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming")) / "Code"
    else:
        raise RuntimeError("The CanMV bridge supports Windows/macOS/Linux")
    return root / "User/globalStorage/kendryte747.canmv-vscode"


def bridge_endpoint(storage):
    identity_path = str(Path(storage))
    if sys.platform == "win32" and len(identity_path) > 1 and identity_path[1] == ":":
        # VS Code Uri.fsPath lowercases the drive letter, but preserves the rest.
        identity_path = identity_path[0].lower() + identity_path[1:]
    identity = hashlib.sha256(identity_path.encode()).hexdigest()[:20]
    if sys.platform == "win32":
        return "\\\\.\\pipe\\canmv-mcp-" + identity
    return str(Path(tempfile.gettempdir()) / ("canmv-mcp-" + identity + ".sock"))


class CanMVBridge:
    def __init__(self, storage, endpoint=None):
        self.storage = Path(storage)
        self.endpoint = endpoint or bridge_endpoint(self.storage)
        self.channel = None
        self.reader = None
        self.next_id = 0
        self.output = deque()
        self.frame_handler = None
        self.snapshot = {}

    def __enter__(self):
        token = (self.storage / "mcp-bridge-token").read_text().strip()
        if sys.platform == "win32":
            from pc_pipe import NamedPipeChannel
            self.channel = NamedPipeChannel()
        else:
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
        if not self.snapshot.get("board"):
            raise ConnectionError(CONNECTION_HELP)

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
            # Keep JPEG delivery separate from terminal events for the live bridge.
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
