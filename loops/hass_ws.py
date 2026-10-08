"""Core's websocket API through the Supervisor, with the add-on's own token. Standard library only:
the handshake, masked client frames, fragmented and control frames (RFC 6455), auth, calls and
event subscriptions."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import itertools
import json
import os
import socket
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class CoreError(Exception):
    """Core answered a call with success false, or the connection could not be used."""


class CoreSocket:
    def __init__(self, token: str, host: str = "supervisor", port: int = 80, path: str = "/core/websocket", timeout: float = 120):
        self.token, self.host, self.port, self.path, self.timeout = token, host, port, path, timeout
        self.sock: socket.socket | None = None
        self.ids = itertools.count(1)
        self.buf = b""

    def __enter__(self) -> CoreSocket:
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {self.path} HTTP/1.1\r\nHost: {self.host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buf:
            self.buf += self._read_some()
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise CoreError(f"websocket upgrade refused: {lines[0]}")
        headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != accept:
            raise CoreError("websocket accept key does not match")
        first = self.recv()
        if first.get("type") != "auth_required":
            raise CoreError(f"expected auth_required, got {first.get('type')}")
        self.send({"type": "auth", "access_token": self.token})
        answer = self.recv()
        if answer.get("type") != "auth_ok":
            raise CoreError(f"auth refused: {answer.get('type')}")

    def close(self) -> None:
        if self.sock:
            with contextlib.suppress(OSError):
                self._frame(0x8, b"")
            self.sock.close()
            self.sock = None

    def _read_some(self) -> bytes:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise CoreError("connection closed")
        return chunk

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            self.buf += self._read_some()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode: int, payload: bytes) -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, doc: dict) -> None:
        self._frame(0x1, json.dumps(doc).encode())

    def recv(self) -> dict:
        """The next text message, its fragments joined; pings are answered, a close raises."""
        parts: list[bytes] = []
        while True:
            b0, b1 = self._read(2)
            fin, opcode, n = b0 & 0x80, b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else b""
            payload = self._read(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:
                raise CoreError("server closed the websocket")
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if fin:
                return json.loads(b"".join(parts))

    def call(self, kind: str, **fields) -> object:
        """Send one command and return its result; a message for another id (an event) is skipped."""
        mid = next(self.ids)
        self.send({"id": mid, "type": kind, **fields})
        while True:
            msg = self.recv()
            if msg.get("id") == mid and msg.get("type") == "result":
                if not msg.get("success"):
                    raise CoreError(f"{kind}: {msg.get('error')}")
                return msg.get("result")

    def subscribe(self, event_type: str) -> int:
        mid = next(self.ids)
        self.send({"id": mid, "type": "subscribe_events", "event_type": event_type})
        while True:
            msg = self.recv()
            if msg.get("id") == mid and msg.get("type") == "result":
                if not msg.get("success"):
                    raise CoreError(f"subscribe {event_type}: {msg.get('error')}")
                return mid

    def events(self, sub_id: int):
        """Event payloads of one subscription, as they arrive."""
        while True:
            msg = self.recv()
            if msg.get("id") == sub_id and msg.get("type") == "event":
                yield msg["event"]
