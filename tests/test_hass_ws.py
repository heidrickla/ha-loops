"""The websocket client against a local server speaking RFC 6455 as Core does: auth, a call whose
answer arrives in fragments around a ping, a payload past 65535 bytes, a refusal."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "loops"))
import hass_ws


def frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([n])
    elif n < 65536:
        head += bytes([126]) + struct.pack(">H", n)
    else:
        head += bytes([127]) + struct.pack(">Q", n)
    return head + payload


def read_frame(conn: socket.socket) -> tuple[int, bytes]:
    def take(n: int) -> bytes:
        out = b""
        while len(out) < n:
            out += conn.recv(n - len(out))
        return out

    b0, b1 = take(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", take(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", take(8))[0]
    assert b1 & 0x80, "a client frame must be masked"
    mask = take(4)
    return b0 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(take(n)))


def serve(script):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    seen: dict = {}

    def run():
        conn, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += conn.recv(4096)
        key = next(line.split(":", 1)[1].strip() for line in req.decode().split("\r\n") if line.lower().startswith("sec-websocket-key"))
        accept = base64.b64encode(hashlib.sha1((key + hass_ws.GUID).encode()).digest()).decode()
        conn.sendall(f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n".encode())
        conn.sendall(frame(0x1, json.dumps({"type": "auth_required"}).encode()))
        script(conn, seen)
        conn.close()
        srv.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    seen["thread"] = thread
    return port, seen


def test_a_call_answered_in_fragments_around_a_ping_returns_its_result():
    big = "x" * 70000

    def script(conn, seen):
        _, auth = read_frame(conn)
        seen["auth"] = json.loads(auth)
        conn.sendall(frame(0x1, json.dumps({"type": "auth_ok"}).encode()))
        _, call = read_frame(conn)
        seen["call"] = json.loads(call)
        body = json.dumps({"id": seen["call"]["id"], "type": "result", "success": True, "result": {"big": big}}).encode()
        conn.sendall(frame(0x1, json.dumps({"id": 99, "type": "event", "event": {}}).encode()))
        conn.sendall(frame(0x1, body[:100], fin=False) + frame(0x9, b"hi") + frame(0x0, body[100:]))
        seen["pong"] = read_frame(conn)

    port, seen = serve(script)
    with hass_ws.CoreSocket("tok", host="127.0.0.1", port=port, timeout=10) as ws:
        got = ws.call("recorder/statistics_during_period", period="hour")
    seen["thread"].join(10)
    assert got == {"big": big}
    assert seen["auth"] == {"type": "auth", "access_token": "tok"}
    assert seen["call"]["type"] == "recorder/statistics_during_period" and seen["call"]["period"] == "hour"
    assert seen["pong"] == (0xA, b"hi")


def test_a_refused_auth_and_a_failed_call_raise():
    def refuse(conn, seen):
        read_frame(conn)
        conn.sendall(frame(0x1, json.dumps({"type": "auth_invalid"}).encode()))

    port, _ = serve(refuse)
    with pytest.raises(hass_ws.CoreError, match="auth refused"):
        hass_ws.CoreSocket("bad", host="127.0.0.1", port=port, timeout=10).connect()

    def fail(conn, seen):
        read_frame(conn)
        conn.sendall(frame(0x1, json.dumps({"type": "auth_ok"}).encode()))
        _, call = read_frame(conn)
        mid = json.loads(call)["id"]
        conn.sendall(frame(0x1, json.dumps({"id": mid, "type": "result", "success": False, "error": {"code": "unknown_command"}}).encode()))

    port, _ = serve(fail)
    with hass_ws.CoreSocket("tok", host="127.0.0.1", port=port, timeout=10) as ws, pytest.raises(hass_ws.CoreError, match="unknown_command"):
        ws.call("nope")
