#!/usr/bin/env python3
"""kick_pusher_probe.py - verify a Kick Puser app key without any pip install.

Kick chat runs on a public Pusher app. When Kick rotates the app key, every
client that still sends the old key dies with:

    pusher:error code=4001 "App key ... not in this cluster"

This script opens the WebSocket by hand (stdlib only: socket + ssl + base64),
so it works on a Windows box with no websocket-client installed, and tells you
which key is alive:

    python kick_pusher_probe.py                      # probes the current + legacy key
    python kick_pusher_probe.py --channel 123456     # also subscribe to chatrooms.123456.v2
    python kick_pusher_probe.py --json               # machine readable
    python kick_pusher_probe.py --selftest           # offline test of the protocol code
    python kick_pusher_probe.py --selftest --offline # skip the loopback server test

Exit codes: 0 = at least one key connected, 2 = nothing connected / error.

Frame format reference: RFC 6455 sections 5.2 - 5.7, Pusher protocol 7.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time

DEFAULT_NEW_KEY = "34bf7a0ff419a2a775b9"     # Kick announcement 2026-10-08
DEFAULT_OLD_KEY = "32cbd69e4b950bf97679"     # revoked / being revoked
DEFAULT_CLUSTER = "us2"
DEFAULT_PATH_VERSION = "8.4.0"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONT = 0x0
OPCODE_TEXT = 0x1
OPCODE_BIN = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA


# --------------------------------------------------------------------------
# RFC 6455 framing
# --------------------------------------------------------------------------

def build_frame(payload: bytes, opcode: int = OPCODE_TEXT, mask: bool = True, fin: bool = True) -> bytes:
    """Build a WebSocket frame. Clients MUST mask, servers MUST NOT."""
    b1 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    header = bytearray([b1])
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < (1 << 16):
        header.append(mask_bit | 126)
        header += struct.pack("!H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack("!Q", length)
    if not mask:
        return bytes(header) + payload
    key = os.urandom(4)
    masked = bytes(payload[i] ^ key[i % 4] for i in range(len(payload)))
    return bytes(header) + key + masked


class FrameReader:
    """Minimal WS frame reader that also replies to control frames."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = bytearray()
        self.fragments: list[bytes] = []
        self.fragmented_opcode = None
        self.closed = False
        self.last_close_code = None

    def _fill(self, n: int) -> bool:
        while len(self.buf) < n:
            try:
                chunk = self.sock.recv(65536)
            except (socket.timeout, TimeoutError):
                return False
            except OSError:
                self.closed = True
                return False
            if not chunk:
                self.closed = True
                return False
            self.buf += chunk
        return True

    def _take(self, n: int) -> bytes:
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _send(self, payload: bytes, opcode: int) -> None:
        try:
            self.sock.sendall(build_frame(payload, opcode=opcode, mask=True))
        except OSError:
            self.closed = True

    def next_message(self):
        """Return (kind, text) where kind is 'text' or 'close', or None on timeout."""
        while True:
            if not self._fill(2):
                return None
            b1, b2 = self.buf[0], self.buf[1]
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            offset = 2
            if length == 126:
                if not self._fill(offset + 2):
                    return None
                length = struct.unpack("!H", bytes(self.buf[offset:offset + 2]))[0]
                offset += 2
            elif length == 127:
                if not self._fill(offset + 8):
                    return None
                length = struct.unpack("!Q", bytes(self.buf[offset:offset + 8]))[0]
                offset += 8
            mask_key = b""
            if masked:
                if not self._fill(offset + 4):
                    return None
                mask_key = bytes(self.buf[offset:offset + 4])
                offset += 4
            if not self._fill(offset + length):
                return None
            self._take(offset)
            payload = self._take(length)
            if masked and mask_key:
                payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(len(payload)))

            if opcode == OPCODE_CLOSE:
                code = None
                if len(payload) >= 2:
                    code = struct.unpack("!H", payload[:2])[0]
                self.last_close_code = code
                self.closed = True
                self._send(payload[:2] or struct.pack("!H", 1000), OPCODE_CLOSE)
                return ("close", code)
            if opcode == OPCODE_PING:
                self._send(payload, OPCODE_PONG)
                continue
            if opcode == OPCODE_PONG:
                continue
            if opcode in (OPCODE_TEXT, OPCODE_BIN):
                if fin:
                    return ("text", payload.decode("utf-8", "replace"))
                self.fragmented_opcode = opcode
                self.fragments = [payload]
                continue
            if opcode == OPCODE_CONT:
                self.fragments.append(payload)
                if fin:
                    joined = b"".join(self.fragments).decode("utf-8", "replace")
                    self.fragments = []
                    self.fragmented_opcode = None
                    return ("text", joined)
                continue
            return None


def parse_frame(data: bytes):
    """Decode exactly one frame from bytes - used by the self test."""
    if len(data) < 2:
        return None
    b1, b2 = data[0], data[1]
    fin = bool(b1 & 0x80)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    idx = 2
    if length == 126:
        length = struct.unpack("!H", data[idx:idx + 2])[0]
        idx += 2
    elif length == 127:
        length = struct.unpack("!Q", data[idx:idx + 8])[0]
        idx += 8
    mask_key = b""
    if masked:
        mask_key = data[idx:idx + 4]
        idx += 4
    payload = data[idx:idx + length]
    if masked and mask_key:
        payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(len(payload)))
    return {"fin": fin, "opcode": opcode, "masked": masked, "payload": payload}


# --------------------------------------------------------------------------
# Pusher client
# --------------------------------------------------------------------------

def open_socket(url: str, timeout: float):
    """Handshake by hand and return (socket, leftover_bytes).

    Pusher may send the 101 headers and the first websocket frame in the same
    TCP segment, so leftover bytes after the header block MUST be kept.
    """
    if url.startswith("wss://"):
        rest = url[len("wss://"):]
        use_tls = True
    elif url.startswith("ws://"):
        rest = url[len("ws://"):]
        use_tls = False
    else:
        raise ValueError("url must start with ws:// or wss://")
    hostport, _, path = rest.partition("/")
    path = "/" + path
    host, _, port_s = hostport.partition(":")
    port = int(port_s) if port_s else (443 if use_tls else 80)
    raw = socket.create_connection((host, port), timeout=timeout)
    if use_tls:
        ctx = ssl.create_default_context()
        raw = ctx.wrap_socket(raw, server_hostname=host)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {hostport}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "User-Agent: hermes-kick-probe/1.0\r\n"
        "\r\n"
    )
    raw.sendall(req.encode())
    raw.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = raw.recv(4096)
        if not chunk:
            raise ConnectionError("server closed during handshake: " + buf.decode("latin-1", "replace")[:300])
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    text = head.decode("latin-1")
    status = text.split("\r\n")[0]
    if " 101" not in status:
        raise ConnectionError("handshake refused: " + status)
    import hashlib
    expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    if expected.lower() not in text.lower():
        raise ConnectionError("server sent a wrong Sec-WebSocket-Accept (not a websocket server?)")
    return raw, rest


def probe(key: str, cluster: str, channel: str | None = None, timeout: float = 12.0,
          url: str | None = None, keepalive_ping: bool = False) -> dict:
    target = url or f"wss://ws-{cluster}.pusher.com/app/{key}?protocol=7&client=js&version={DEFAULT_PATH_VERSION}&flash=false"
    result = {
        "key": key, "cluster": cluster, "url": target, "connected": False,
        "subscribed": False, "message_seen": False, "socket_id": None,
        "error_code": None, "error_message": None, "close_code": None,
        "frames": 0, "ms": 0,
    }
    started = time.time()
    sock = None
    try:
        sock, leftover = open_socket(target, timeout)
        reader = FrameReader(sock)
        if leftover:
            reader.buf += leftover
        deadline = time.time() + timeout
        subscribe_sent = False
        while time.time() < deadline:
            sock.settimeout(max(0.5, deadline - time.time()))
            frame = reader.next_message()
            if frame is None:
                if reader.closed:
                    result["close_code"] = reader.last_close_code
                    break
                continue
            kind, text = frame
            if kind == "close":
                result["close_code"] = text
                break
            result["frames"] += 1
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            event = msg.get("event") or ""
            data = msg.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    data = {"raw": data}
            if event == "pusher:connection_established":
                result["connected"] = True
                if isinstance(data, dict):
                    result["socket_id"] = data.get("socket_id")
                if channel and not subscribe_sent:
                    subscribe_sent = True
                    sub = json.dumps({"event": "pusher:subscribe", "data": {"channel": channel}})
                    sock.sendall(build_frame(sub.encode(), OPCODE_TEXT, mask=True))
                    deadline = time.time() + timeout
                    continue
                if not channel and not keepalive_ping:
                    break
                if keepalive_ping:
                    sock.sendall(build_frame(json.dumps({"event": "pusher:ping", "data": {}}).encode(),
                                             OPCODE_TEXT, mask=True))
                    break
            elif event == "pusher:error":
                if isinstance(data, dict):
                    result["error_code"] = data.get("code")
                    result["error_message"] = data.get("message")
                break
            elif event == "pusher_internal:subscription_succeeded":
                result["subscribed"] = True
                break
            elif event.startswith("App\\Events\\"):
                result["message_seen"] = True
                break
            elif event == "pusher:pong":
                break
    except Exception as exc:  # noqa: BLE001 - report, never crash the caller
        result["error_message"] = f"{type(exc).__name__}: {exc}"
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
    result["ms"] = int((time.time() - started) * 1000)
    return result


def describe(r: dict) -> str:
    tag = f"[{r['cluster']}] {r['key']}"
    if r["connected"]:
        extra = []
        if r["socket_id"]:
            extra.append(f"socket_id={r['socket_id']}")
        if r["subscribed"]:
            extra.append("subscription_succeeded")
        if r["message_seen"]:
            extra.append("chat message received")
        extra.append(f"{r['ms']} ms")
        return f"OK   {tag} -> connected ({', '.join(extra)})"
    if r["error_code"]:
        return f"FAIL {tag} -> pusher:error code={r['error_code']} {r['error_message']}"
    return f"WARN {tag} -> not connected: {r['error_message']} (close={r['close_code']})"


# --------------------------------------------------------------------------
# Self test
# --------------------------------------------------------------------------

def _fake_pusher_server(port_holder: list, stop: threading.Event) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port_holder.append(srv.getsockname()[1])
    srv.settimeout(5)
    try:
        conn, _ = srv.accept()
    except (socket.timeout, OSError):
        srv.close()
        return
    try:
        conn.settimeout(5)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk
        head = buf.split(b"\r\n\r\n")[0].decode("latin-1")
        import hashlib
        key = ""
        for line in head.split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        conn.sendall((
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode())
        conn.sendall(build_frame(json.dumps({
            "event": "pusher:connection_established",
            "data": json.dumps({"socket_id": "1234.5678", "activity_timeout": 120}),
        }).encode(), OPCODE_TEXT, mask=False))
        # wait for the subscribe frame (masked, client -> server)
        reader = FrameReader(conn)
        conn.settimeout(5)
        frame = reader.next_message()
        if frame and frame[0] == "text":
            conn.sendall(build_frame(json.dumps({
                "event": "pusher_internal:subscription_succeeded",
                "data": json.dumps({"channel": "chatrooms.123456.v2"}),
            }).encode(), OPCODE_TEXT, mask=False))
        time.sleep(0.3)
    finally:
        try:
            conn.close()
        except OSError:
            pass
        srv.close()


def selftest(offline: bool = False) -> int:
    fails = []
    passes = 0

    def check(name, cond, detail=""):
        nonlocal passes
        if cond:
            passes += 1
            print(f"  [PASS] {name}")
        else:
            fails.append(f"{name} {detail}")
            print(f"  [FAIL] {name} {detail}")

    # frame round trip
    for payload in (b"a", b"x" * 125, b"y" * 126, b"z" * 70000):
        frame = build_frame(payload, OPCODE_TEXT, mask=True)
        decoded = parse_frame(frame)
        check(f"masked frame round trip ({len(payload)} bytes)",
              decoded and decoded["payload"] == payload and decoded["masked"] and decoded["opcode"] == OPCODE_TEXT)
    frame = build_frame(b"hello", OPCODE_TEXT, mask=False)
    decoded = parse_frame(frame)
    check("unmasked server frame", decoded["payload"] == b"hello" and not decoded["masked"])

    # fragmentation + control frames through the reader
    class FakeSock:
        def __init__(self, chunks):
            self.chunks = list(chunks)
            self.sent = []
        def recv(self, _n):
            return self.chunks.pop(0) if self.chunks else b""
        def sendall(self, data):
            self.sent.append(data)

    part1 = build_frame(b'{"event":"pusher', OPCODE_TEXT, mask=False, fin=False)
    ping = build_frame(b"hb", OPCODE_PING, mask=False)
    part2 = build_frame(b':connection_established"}', OPCODE_CONT, mask=False, fin=True)
    sock = FakeSock([part1, ping, part2])
    reader = FrameReader(sock)
    frame = reader.next_message()
    check("fragment reassembly", frame and frame[0] == "text" and "connection_established" in frame[1])
    check("ping answered with masked pong", bool(sock.sent) and parse_frame(sock.sent[0])["opcode"] == OPCODE_PONG
          and parse_frame(sock.sent[0])["masked"])

    close = build_frame(struct.pack("!H", 4001) + b"App key not in this cluster", OPCODE_CLOSE, mask=False)
    sock2 = FakeSock([close])
    reader2 = FrameReader(sock2)
    frame = reader2.next_message()
    check("close frame with code", frame and frame[0] == "close" and frame[1] == 4001 and reader2.last_close_code == 4001)

    if not offline:
        holder: list = []
        stop = threading.Event()
        t = threading.Thread(target=_fake_pusher_server, args=(holder, stop), daemon=True)
        t.start()
        for _ in range(50):
            if holder:
                break
            time.sleep(0.1)
        if not holder:
            check("loopback pusher server", False, "server did not start")
        else:
            r = probe("dummy-key", "us2", channel="chatrooms.123456.v2",
                      timeout=6, url=f"ws://127.0.0.1:{holder[0]}/app/dummy-key?protocol=7")
            check("end-to-end handshake + subscribe over ws://",
                  r["connected"] and r["subscribed"] and r["socket_id"] == "1234.5678",
                  f"-> {describe(r)}")
        t.join(timeout=3)

    print()
    print(f"self test: {passes} passed, {len(fails)} failed")
    for f in fails:
        print("  -", f)
    return 2 if fails else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Probe Kick's Pusher websocket app keys (stdlib only).")
    p.add_argument("--key", default=DEFAULT_NEW_KEY, help="key to test (default: kick's new key)")
    p.add_argument("--legacy-key", default=DEFAULT_OLD_KEY, help="second key to test (default: the revoked one)")
    p.add_argument("--cluster", default=DEFAULT_CLUSTER)
    p.add_argument("--channel", default=None, help="chatroom id or full channel name to subscribe to")
    p.add_argument("--timeout", type=float, default=12.0)
    p.add_argument("--json", action="store_true", help="print machine readable result")
    p.add_argument("--only", choices=("new", "legacy"), help="probe a single key")
    p.add_argument("--url", default=None, help="override the ws url (testing)")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--offline", action="store_true", help="with --selftest: skip the loopback test")
    args = p.parse_args(argv)

    if args.selftest:
        return selftest(offline=args.offline)

    channel = None
    if args.channel:
        channel = args.channel if args.channel.startswith("chatrooms.") else f"chatrooms.{args.channel}.v2"

    keys = []
    if args.only != "legacy":
        keys.append(args.key)
    if args.only != "new" and args.legacy_key and args.legacy_key not in keys:
        keys.append(args.legacy_key)

    results = [probe(k, args.cluster, channel=channel, timeout=args.timeout, url=args.url) for k in keys]

    if args.json:
        print(json.dumps({"results": results, "source": "Kick announcement x.com/notjoker 2026-10-08"}, indent=2))
    else:
        print("Kick / Pusher key probe")
        for r in results:
            print("  " + describe(r))
        good = [r["key"] for r in results if r["connected"]]
        if good:
            print(f"\n=> use this key: {good[0]}")
        else:
            print("\n=> no key connected. check the network/TLS/proxy, or Kick rotated again "
                  "(https://x.com/notjoker).")
    return 0 if any(r["connected"] for r in results) else 2


if __name__ == "__main__":
    sys.exit(main())
