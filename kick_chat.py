#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kick_chat.py - เครื่องมือ Kick chat (Pusher WebSocket) แบบไม่ต้องติดตั้งแพ็กเกจเพิ่ม

ทำอะไรได้บ้าง:
  probe     ทดสอบทุก Pusher cluster อัตโนมัติว่า app key ของ Kick อยู่ cluster ไหน
            (แก้ปัญหา "App key ... not in this cluster" ได้ตรงจุด)
  discover  พยายามดึง wss URL จริงจากเว็บ kick.com (best-effort, อาจติด Cloudflare)
  resolve   หา channel_id / chatroom_id จากชื่อช่อง (slug)
  listen    ต่อ WebSocket แล้วอ่านแชทสด ๆ (มี keepalive + reconnect)
  send      ส่งข้อความเข้าแชทผ่าน API ทางการ (POST /public/v1/chat, ใช้ user token)
  doctor    ตรวจทุกอย่างรวบรัด: config, cluster ที่ใช้ได้, token ที่มีอยู่

ตัวอย่าง:
  python kick_chat.py probe
  python kick_chat.py listen --channel yourslug
  python kick_chat.py listen --chatroom 123456 --json
  python kick_chat.py send --channel yourslug --message "สวัสดีครับ"

ข้อมูลอ้างอิง: Kick ใช้ Pusher app key สาธารณะ 32cbd69e4b950bf97679 บน cluster us2
(ค่า key ไม่ได้มาจาก Kick Developer Portal — portal ให้แค่ OAuth client id/secret)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# ค่าคงที่
# ---------------------------------------------------------------------------
DEFAULT_APP_KEY = "32cbd69e4b950bf97679"          # key สาธารณะที่ Kick ใช้บนเว็บ
FALLBACK_APP_KEYS = ("34bf7a0ff419a2a775b9",)     # key เก่าที่บางโปรเจกต์ใช้สำรอง
DEFAULT_CLUSTER = "us2"

# ชื่อ cluster ของ Pusher -> host ของ WebSocket
CLUSTER_HOSTS = {
    "us2": "ws-us2.pusher.com",
    "us3": "ws-us3.pusher.com",
    "us1": "ws-us1.pusher.com",
    "eu": "ws-eu.pusher.com",
    "ap1": "ws-ap1.pusher.com",
    "ap2": "ws-ap2.pusher.com",
    "ap3": "ws-ap3.pusher.com",
    "ap4": "ws-ap4.pusher.com",
    "mt1": "ws-mt1.pusher.com",
    "sa1": "ws-sa1.pusher.com",
    "in1": "ws-in1.pusher.com",
}
# ลำดับที่ใช้ไล่ทดสอบ (us2 มาก่อนเพราะ Kick ใช้ตัวนี้)
PROBE_ORDER = ["us2", "us3", "us1", "eu", "ap1", "ap2", "ap3", "ap4", "mt1", "sa1", "in1"]

PUSHER_CLIENT = "js"
PUSHER_VERSION = "8.4.0-rc2"
PUSHER_ORIGIN = "https://kick.com"

KICK_CHANNEL_API = "https://kick.com/api/v2/channels/{slug}"
KICK_HOME = "https://kick.com/"
API_BASE = "https://api.kick.com"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,th;q=0.8",
    "Cache-Control": "no-cache",
}

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_NAME = "kick_chat_config.json"

WS_URL_RE = re.compile(r"wss?://ws-([a-z0-9]+)\.pusher\.com/app/([0-9a-f]{12,40})", re.I)
APP_KEY_RE = re.compile(r"[\"']?(?:key|appKey|app_key|PUSHER_APP_KEY)[\"']?\s*[:=]\s*[\"']([0-9a-f]{12,40})[\"']", re.I)


# ---------------------------------------------------------------------------
# ยูทิลิตี้พื้นฐาน
# ---------------------------------------------------------------------------
def _fix_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[attr-defined]
        except Exception:
            pass


def stamp() -> str:
    return time.strftime("%H:%M:%S")


def log(msg: str) -> None:
    """ข้อความสถานะออกทาง stderr เพื่อให้ stdout สะอาดพอสำหรับ pipe JSON"""
    print(f"[{stamp()}] {msg}", file=sys.stderr)


def ok(msg: str) -> None:
    print(f"[OK]  {msg}")


def info(msg: str) -> None:
    print(f"[..] {msg}")


def warn(msg: str) -> None:
    print(f"[!]   {msg}", file=sys.stderr)


def fail(msg: str, code: int = 1):
    print(f"[X]   {msg}", file=sys.stderr)
    raise SystemExit(code)


def mask(value: str | None, keep: int = 8) -> str:
    if not value:
        return "(ว่าง)"
    return value[:keep] + "..." if len(value) > keep else value


# ---------------------------------------------------------------------------
# WebSocket (RFC 6455) แบบ stdlib
# ---------------------------------------------------------------------------
class WebSocketError(Exception):
    pass


class WebSocketClosed(WebSocketError):
    def __init__(self, code: int = 0, reason: str = ""):
        super().__init__(f"ปิดการเชื่อมต่อ (code {code} {reason})".strip())
        self.code, self.reason = code, reason


def build_ws_url(cluster: str, app_key: str = DEFAULT_APP_KEY) -> str:
    host = CLUSTER_HOSTS.get(cluster, f"ws-{cluster}.pusher.com")
    return (
        f"wss://{host}/app/{app_key}"
        f"?protocol=7&client={PUSHER_CLIENT}&version={PUSHER_VERSION}&flash=false"
    )


def parse_ws_url(url: str) -> tuple[str, str, str]:
    """แยก (cluster, app_key, host) จาก URL ที่คัดลอกจาก DevTools"""
    parsed = urllib.parse.urlparse(url.strip())
    m = re.match(r"^ws-([a-z0-9]+)\.pusher\.com$", (parsed.hostname or ""), re.I)
    cluster = m.group(1) if m else ""
    key = ""
    km = re.search(r"/app/([0-9a-zA-Z]+)", parsed.path or "")
    if km:
        key = km.group(1)
    if not cluster and "pusher" in (parsed.hostname or ""):
        cluster = DEFAULT_CLUSTER
    return cluster, key, parsed.hostname or ""


def extract_pusher_from_text(text: str) -> tuple[str, str] | None:
    """หา cluster + app key จาก HTML/JS ของ Kick"""
    m = WS_URL_RE.search(text)
    if m:
        return m.group(1).lower(), m.group(2)
    m2 = APP_KEY_RE.search(text)
    if m2:
        return "", m2.group(1)
    return None


class WebSocket:
    """WebSocket client แบบพอใช้: text frames, ping/pong, close (ไม่รองรับ permessage-deflate)"""

    def __init__(self, sock: socket.socket, leftover: bytes = b"", timeout: float = 30.0):
        self.sock = sock
        self._buf = bytearray(leftover)
        self.timeout = timeout
        # ข้อมูลที่เก็บไว้เพื่อการวินิจฉัย (ใช้โดยคำสั่ง fix-config)
        self.handshake_status = ""
        self.handshake_headers: dict[str, str] = {}
        self.host = ""
        self.resolved_ips: list[str] = []

    # --- อ่าน/เขียนระดับล่าง ---
    def _recv_into_buf(self, timeout: float | None) -> None:
        self.sock.settimeout(self.timeout if timeout is None else timeout)
        chunk = self.sock.recv(65536)
        if not chunk:
            raise WebSocketClosed(1006, "peer ปิดการเชื่อมต่อ")
        self._buf += chunk

    def _read_exact(self, n: int, timeout: float | None = None) -> bytes:
        while len(self._buf) < n:
            self._recv_into_buf(timeout)
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def send_frame(self, opcode: int, payload: bytes = b"") -> None:
        header = bytes([0x80 | opcode])
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def send_text(self, text: str) -> None:
        self.send_frame(0x1, text.encode("utf-8"))

    def ping(self, payload: bytes = b"") -> None:
        self.send_frame(0x9, payload)

    def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            self.send_frame(0x8, struct.pack("!H", code) + reason.encode("utf-8"))
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    # --- อ่าน frame ---
    def _read_frame(self, timeout: float | None = None) -> tuple[bool, int, bytes]:
        b1, b2 = self._read_exact(2, timeout)
        fin = bool(b1 & 0x80)
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2, timeout))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8, timeout))[0]
        mask = self._read_exact(4, timeout) if masked else b""
        payload = self._read_exact(length, timeout) if length else b""
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return fin, opcode, payload

    def recv_text(self, timeout: float | None = None) -> str:
        """คืนข้อความถัดไป (ตอบ ping/pong ให้อัตโนมัติ), โยน WebSocketClosed เมื่อปิด"""
        parts: list[bytes] = []
        while True:
            fin, opcode, payload = self._read_frame(timeout)
            if opcode == 0x9:  # ping -> pong
                self.send_frame(0xA, payload)
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode == 0x8:  # close
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
                reason = payload[2:].decode("utf-8", "replace")
                raise WebSocketClosed(code, reason)
            if opcode in (0x1, 0x2, 0x0):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode("utf-8", "replace")


def ws_connect(url: str, timeout: float = 10.0, origin: str = PUSHER_ORIGIN) -> WebSocket:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    scheme = parsed.scheme.lower()
    port = parsed.port or (443 if scheme in ("wss", "https") else 80)
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")

    resolved: list[str] = []
    try:
        resolved = resolve_host(host, port)
    except OSError:
        pass

    sock = socket.create_connection((host, port), timeout=timeout)
    if scheme in ("wss", "https"):
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Origin: {origin}\r\n"
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) kick_chat.py\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("utf-8"))

    buf = b""
    deadline = time.time() + timeout
    while b"\r\n\r\n" not in buf:
        if time.time() > deadline:
            raise WebSocketError("หมดเวลารอ handshake")
        sock.settimeout(max(0.5, deadline - time.time()))
        chunk = sock.recv(4096)
        if not chunk:
            raise WebSocketError("เซิร์ฟเวอร์ปิดระหว่าง handshake")
        buf += chunk
    head, _, leftover = buf.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n")[0].decode("latin-1", "replace")

    if " 101 " not in status_line:
        try:
            sock.close()
        except Exception:
            pass
        raise WebSocketError(f"handshake ไม่สำเร็จ: {status_line}")

    # ตรวจ Sec-WebSocket-Accept ตามสเปก (ถ้ามี header)
    expected = base64.b64encode(
        __import__("hashlib").sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    headers: dict[str, str] = {}
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        hname = name.strip().decode("latin-1", "replace")
        hvalue = value.strip().decode("latin-1", "replace")
        if hname:
            headers[hname] = hvalue
        if hname.lower() == "sec-websocket-accept" and hvalue != expected:
            warn("Sec-WebSocket-Accept ไม่ตรงตามสเปก (ทำงานต่อ)")

    client = WebSocket(sock, leftover, timeout=timeout)
    client.handshake_status = status_line
    client.handshake_headers = headers
    client.host = host
    client.resolved_ips = resolved
    return client


# ---------------------------------------------------------------------------
# Pusher protocol 7
# ---------------------------------------------------------------------------
class PusherError(Exception):
    pass


class PusherWrongCluster(PusherError):
    """error 4001: 'App key ... not in this cluster' = ต่อผิด cluster"""


class PusherClient:
    def __init__(self, url: str, timeout: float = 15.0, idle_ping: float | None = None):
        self.url = url
        self.timeout = timeout
        self.ws: WebSocket | None = None
        self.socket_id = ""
        self.activity_timeout = 120
        self.idle_ping = idle_ping
        self.handshake: dict = {}
        self.last_error: str | None = None

    def _record_handshake(self) -> None:
        ws = self.ws
        if ws is None:
            return
        self.handshake = {
            "host": ws.host,
            "ips": list(ws.resolved_ips),
            "status": ws.handshake_status,
            "headers": dict(ws.handshake_headers),
        }

    # --- lifecycle ---
    def connect(self) -> dict:
        self.ws = ws_connect(self.url, self.timeout)
        self._record_handshake()
        deadline = time.time() + self.timeout
        while True:
            remaining = max(0.5, deadline - time.time())
            raw = self.ws.recv_text(remaining)
            event = json.loads(raw)
            name = event.get("event")
            data = self._data(event)
            if name == "pusher:connection_established":
                self.socket_id = (data or {}).get("socket_id", "")
                self.activity_timeout = int((data or {}).get("activity_timeout", 120) or 120)
                if self.idle_ping is None:
                    self.idle_ping = max(5.0, min(30.0, self.activity_timeout / 4))
                return data or {}
            if name == "pusher:error":
                message = str((data or {}).get("message", data))
                self.last_error = message
                if "not in this cluster" in message.lower():
                    raise PusherWrongCluster(message)
                raise PusherError(message)
            if time.time() > deadline:
                raise PusherError("ไม่ได้ connection_established จาก Pusher")

    @staticmethod
    def _data(event: dict):
        data = event.get("data")
        if isinstance(data, str):
            if not data.strip():
                return {}
            try:
                return json.loads(data)
            except json.JSONDecodeError:
                return {"message": data}
        return data or {}

    def close(self) -> None:
        if self.ws:
            self.ws.close()

    def _send(self, event: str, data: dict) -> None:
        assert self.ws is not None
        self.ws.send_text(json.dumps({"event": event, "data": data}))

    def subscribe(self, channel: str, timeout: float | None = None) -> bool:
        self._send("pusher:subscribe", {"auth": "", "channel": channel})
        deadline = time.time() + (timeout or self.timeout)
        while True:
            raw = self.ws.recv_text(max(0.5, deadline - time.time()))  # type: ignore[union-attr]
            event = json.loads(raw)
            name = event.get("event")
            data = self._data(event)
            if name in ("pusher_internal:subscription_succeeded", "pusher:subscription_succeeded"):
                return True
            if name in ("pusher_internal:subscription_error", "pusher:subscription_error", "pusher:error"):
                raise PusherError(f"subscribe {channel} ไม่สำเร็จ: {json.dumps(data, ensure_ascii=False)}")
            if name == "pusher:ping":
                self._send("pusher:pong", {})
            if time.time() > deadline:
                raise PusherError(f"subscribe {channel} ไม่มีคำตอบ (timeout)")

    def events(self, idle_ping: float | None = None):
        """generator คืน (event_name, data) เรื่อย ๆ พร้อมส่ง ping กันหลุด"""
        assert self.ws is not None
        interval = idle_ping or self.idle_ping or 25.0
        last_rx = time.time()
        while True:
            try:
                raw = self.ws.recv_text(1.0)
            except socket.timeout:
                if time.time() - last_rx >= interval:
                    try:
                        self._send("pusher:ping", {})
                    except (OSError, WebSocketError):
                        raise
                    last_rx = time.time()
                continue
            last_rx = time.time()
            event = json.loads(raw)
            name = event.get("event")
            if name == "pusher:ping":
                self._send("pusher:pong", {})
                continue
            if name == "pusher:pong":
                continue
            if name == "pusher:error":
                data = self._data(event)
                message = str((data or {}).get("message", data))
                if "not in this cluster" in message.lower():
                    raise PusherWrongCluster(message)
                raise PusherError(message)
            if name == "pusher:connection_established":
                data = self._data(event)
                self.activity_timeout = int((data or {}).get("activity_timeout", self.activity_timeout) or self.activity_timeout)
                continue
            yield name, self._data(event)


# ---------------------------------------------------------------------------
# config ไฟล์
# ---------------------------------------------------------------------------
def config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    cfg = SCRIPT_DIR / CONFIG_NAME
    if not cfg.is_file():
        alt = Path.cwd() / CONFIG_NAME
        if alt.is_file():
            return alt
    return cfg


def load_config(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"อ่าน config ไม่ได้: {path} ({exc})")
        return {}
    return data if isinstance(data, dict) else {}


def save_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = load_config(path)
    merged.update({k: v for k, v in data.items() if v not in (None, "")})
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def app_keys_from(args, cfg: dict) -> list[str]:
    keys: list[str] = []
    for source in (getattr(args, "app_key", None), cfg.get("app_key")):
        if not source:
            continue
        for part in str(source).replace(" ", "").split(","):
            if part and part not in keys:
                keys.append(part)
    if not keys:
        keys = [DEFAULT_APP_KEY]
    for extra in FALLBACK_APP_KEYS:
        if extra not in keys:
            keys.append(extra)
    return keys


def clusters_from(args, cfg: dict) -> list[str]:
    raw = getattr(args, "clusters", None) or cfg.get("cluster") or ""
    if raw:
        wanted = [c.strip().lower() for c in re.split(r"[,\s]+", str(raw)) if c.strip()]
        if wanted:
            return wanted
    return list(PROBE_ORDER)


# ---------------------------------------------------------------------------
# คำสั่ง: probe
# ---------------------------------------------------------------------------
def probe_once(url: str, timeout: float) -> tuple[str, str]:
    """คืน (สถานะ, รายละเอียด) โดยสถานะเป็น ok / wrong-cluster / unreachable"""
    client = PusherClient(url, timeout=timeout)
    try:
        data = client.connect()
        return "ok", json.dumps(data, ensure_ascii=False)
    except PusherWrongCluster as exc:
        return "wrong-cluster", str(exc)
    except PusherError as exc:
        return "error", str(exc)
    except (socket.gaierror,) as exc:
        return "unreachable", f"DNS ใช้ไม่ได้ ({exc})"
    except (socket.timeout, TimeoutError):
        return "unreachable", "หมดเวลาเชื่อมต่อ"
    except ssl.SSLError as exc:
        return "unreachable", f"TLS ผิดพลาด ({exc})"
    except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
        return "unreachable", f"{type(exc).__name__}: {exc}"
    except WebSocketError as exc:
        return "unreachable", f"WebSocket: {exc}"
    finally:
        client.close()


def cmd_probe(args) -> int:
    cfg = load_config(config_path(args.config))
    keys = app_keys_from(args, cfg)
    clusters = clusters_from(args, cfg)

    print("=== ทดสอบว่า Kick app key อยู่ cluster ไหน ===")
    print(f"      app key : {', '.join(mask(k, 10) for k in keys)}")
    print(f"      cluster : {', '.join(clusters)}")
    print(f"      หมดเวลา : {args.timeout} วินาที/cluster")
    print()

    winner: dict | None = None
    tested = 0
    for key in keys:
        for cluster in clusters:
            url = build_ws_url(cluster, key)
            status, detail = probe_once(url, args.timeout)
            tested += 1
            if status == "ok":
                print(f"[OK]  cluster {cluster:<4} key {mask(key, 10)}  ->  {detail}")
                if winner is None:
                    winner = {"cluster": cluster, "app_key": key, "ws_url": url, "socket": detail}
                    if not args.all:
                        break
            elif status == "wrong-cluster":
                print(f"[xx]  cluster {cluster:<4} key {mask(key, 10)}  ->  ต่อผิด cluster: {detail}")
            elif status == "error":
                print(f"[!!]  cluster {cluster:<4} key {mask(key, 10)}  ->  Pusher error: {detail}")
            else:
                print(f"[--]  cluster {cluster:<4} key {mask(key, 10)}  ->  เชื่อมต่อไม่ได้: {detail}")
        if winner and not args.all:
            break

    print()
    if not winner:
        warn(f"ทดสอบ {tested} ชุด แล้วไม่พบ cluster ที่ใช้ได้")
        print()
        print("สาเหตุที่เป็นไปได้:")
        print("  1) Kick เปลี่ยน app key/cluster → เปิด https://kick.com/<ช่องของคุณ> แล้วเปิด DevTools (F12)")
        print("     แท็บ Network กรองคำว่า 'pusher' แล้วคัดลอก URL ที่ขึ้นต้น wss://ws-XX.pusher.com/app/...")
        print("     จากนั้นรัน:  python kick_chat.py discover --ws-url \"<URL ที่คัดลอกมา>\"")
        print("  2) เน็ต/ไฟร์วอลล์บล็อก wss (443) → ทดสอบด้วย: curl -I https://ws-us2.pusher.com")
        print("  3) ใช้ VPN/ไฟร์วอลล์ขององค์กรที่ตัด WebSocket")
        return 1

    save_config(config_path(args.config), {
        "cluster": winner["cluster"],
        "app_key": winner["app_key"],
        "ws_url": winner["ws_url"],
    })
    print(f"[OK]  cluster ที่ใช้งานได้ = {winner['cluster']} (บันทึกใน {config_path(args.config)})")
    print(f"      ws url = {winner['ws_url']}")
    print()
    print("เอาไปใส่ config.json ของคุณ:")
    print(json.dumps({
        "pusher": {
            "cluster": winner["cluster"],
            "appKey": winner["app_key"],
            "wsUrl": winner["ws_url"],
            "channelTemplate": "chatrooms.{chatroom_id}.v2",
        }
    }, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# คำสั่ง: discover / resolve
# ---------------------------------------------------------------------------
def http_get(url: str, headers: dict | None = None, timeout: float = 15.0) -> tuple[int, str]:
    hdrs = dict(BROWSER_HEADERS)
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4_000_000).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(20_000).decode("utf-8", "replace")
        except Exception:
            pass
        return exc.code, body
    except urllib.error.URLError as exc:
        raise ConnectionError(str(exc.reason)) from exc


def discover_from_web(slug: str | None, max_scripts: int = 6, timeout: float = 15.0) -> tuple[str, str, str]:
    """พยายามหาที่มาของ wss URL จากหน้าเว็บ Kick คืน (cluster, app_key, แหล่งที่มา)"""
    pages = [f"https://kick.com/{slug}"] if slug else []
    pages.append(KICK_HOME)
    for page in pages:
        try:
            status, body = http_get(page, timeout=timeout)
        except ConnectionError as exc:
            warn(f"ดึง {page} ไม่ได้: {exc}")
            continue
        if status != 200:
            warn(f"{page} ตอบ HTTP {status} (Cloudflare อาจบล็อก — ใช้ DevTools แทน)")
            continue
        found = extract_pusher_from_text(body)
        if found and found[0] and found[1]:
            return found[0], found[1], page
        scripts = re.findall(r'src="(https://[^"]+\.js[^"]*)"', body)[:max_scripts]
        for script in scripts:
            try:
                s_status, s_body = http_get(script, timeout=timeout)
            except ConnectionError:
                continue
            if s_status == 200:
                found = extract_pusher_from_text(s_body)
                if found and found[0] and found[1]:
                    return found[0], found[1], script
    return "", "", ""


def cmd_discover(args) -> int:
    cfg = load_config(config_path(args.config))
    if args.ws_url:
        cluster, key, host = parse_ws_url(args.ws_url)
        if not key:
            fail("อ่าน URL ไม่ได้ — ต้องมีรูปแบบ /app/<APP_KEY> เช่น wss://ws-us2.pusher.com/app/32cbd...")
        print(f"[OK]  อ่านจาก URL ที่วางมา: cluster={cluster or '(ไม่ระบุ)'} host={host} key={mask(key, 12)}")
        status, detail = probe_once(build_ws_url(cluster or DEFAULT_CLUSTER, key), args.timeout)
        print(f"      ทดสอบเชื่อมต่อ: {status} - {detail}")
        save_config(config_path(args.config), {
            "cluster": cluster or DEFAULT_CLUSTER,
            "app_key": key,
            "ws_url": args.ws_url.strip(),
        })
        print(f"[OK]  บันทึกใน {config_path(args.config)}")
        return 0 if status == "ok" else 1

    print("=== ค้นหา Pusher URL จากเว็บ Kick (best-effort) ===")
    cluster, key, source = discover_from_web(args.channel)
    if not (cluster and key):
        warn("หาจากเว็บไม่เจอ (มักติด Cloudflare) — ใช้วิธี DevTools แทน:")
        print("  1) เปิด https://kick.com/" + (args.channel or "<ชื่อช่องของคุณ>"))
        print("  2) กด F12 > แท็บ Network > กรองคำว่า 'pusher'")
        print("  3) รีเฟรชหน้า จะเห็น wss://ws-us2.pusher.com/app/<KEY>?protocol=7&...")
        print("  4) คัดลอก URL ทั้งเส้น แล้วรัน: python kick_chat.py discover --ws-url \"<URL>\"")
        return 1
    print(f"[OK]  เจอจาก {source}: cluster={cluster} key={mask(key, 12)}")
    save_config(config_path(args.config), {"cluster": cluster, "app_key": key})
    return 0


def cmd_resolve(args) -> int:
    slug = args.channel.strip().lstrip("@")
    print(f"=== หา id ของช่อง {slug} ===")
    status, body = 0, ""
    try:
        status, body = http_get(KICK_CHANNEL_API.format(slug=urllib.parse.quote(slug)))
    except ConnectionError as exc:
        warn(f"เชื่อมต่อ kick.com ไม่ได้: {exc}")

    if status == 200 and body.strip().startswith("{"):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {}
        chatroom_id = (data.get("chatroom") or {}).get("id")
        channel_id = data.get("id")
        broadcaster_user_id = data.get("user_id") or (data.get("user") or {}).get("id")
        if chatroom_id:
            print(f"[OK]  channel_id         = {channel_id}")
            print(f"[OK]  chatroom_id        = {chatroom_id}")
            print(f"[OK]  broadcaster_user_id= {broadcaster_user_id}")
            save_config(config_path(args.config), {
                "channel": slug,
                "channel_id": channel_id,
                "chatroom_id": chatroom_id,
                "broadcaster_user_id": broadcaster_user_id,
                "channel_template": f"chatrooms.{chatroom_id}.v2",
            })
            print(f"      บันทึกใน {config_path(args.config)}")
            return 0

    warn(f"ดึงข้อมูลช่องไม่สำเร็จ (HTTP {status or 'n/a'}) — kick.com ป้องกัน Cloudflare")
    print("วิธีเอา id เอง (เร็วที่สุด):")
    print(f"  1) เปิด https://kick.com/{slug} ในเบราว์เซอร์ แล้วกด F12 > Console")
    print(f"  2) พิมพ์: fetch('/api/v2/channels/{slug}').then(r=>r.json()).then(d=>console.log(d.chatroom.id, d.id, d.user_id))")
    print("  3) นำค่ามาใส่: python kick_chat.py resolve --channel " + slug + " --chatroom <chatroom_id>")
    if args.chatroom:
        save_config(config_path(args.config), {"channel": slug, "chatroom_id": args.chatroom})
        print(f"[OK]  บันทึก chatroom_id={args.chatroom} ที่ผู้ใช้ระบุ")
        return 0
    return 1


# ---------------------------------------------------------------------------
# คำสั่ง: listen
# ---------------------------------------------------------------------------
def chat_message_to_dict(name: str, data: dict) -> dict | None:
    if "ChatMessage" not in name and "chatmessage" not in name.lower():
        return None
    if not isinstance(data, dict):
        return None
    if "content" not in data and "message" not in data:
        return None
    sender = data.get("sender") or {}
    identity = sender.get("identity") or {}
    badges = []
    for badge in identity.get("badges") or []:
        if isinstance(badge, dict):
            badges.append(str(badge.get("type") or badge.get("text") or ""))
    return {
        "id": data.get("id") or data.get("message_id"),
        "created_at": data.get("created_at"),
        "chatroom_id": data.get("chatroom_id"),
        "user_id": sender.get("id"),
        "username": sender.get("username"),
        "slug": sender.get("slug"),
        "color": identity.get("color"),
        "badges": badges,
        "content": data.get("content") or data.get("message"),
        "type": data.get("type"),
        "raw": data,
    }


def format_message(msg: dict) -> str:
    who = msg.get("username") or "?"
    badges = f"({','.join(msg['badges'])}) " if msg.get("badges") else ""
    created = msg.get("created_at")
    ts = stamp()
    if isinstance(created, str) and "T" in created:
        ts = created.split("T", 1)[1][:8]
    return f"[{ts}] {badges}{who}: {msg.get('content')}"


def listen_session(ws_url: str, channels: list[str], args) -> int:
    client = PusherClient(ws_url, timeout=args.timeout)
    data = client.connect()
    log(f"เชื่อมต่อ Pusher สำเร็จ (socket_id={data.get('socket_id')}, activity_timeout={client.activity_timeout}s)")

    subscribed = None
    for channel in channels:
        try:
            client.subscribe(channel)
            subscribed = channel
            log(f"subscribe สำเร็จ: {channel}")
            break
        except PusherError as exc:
            warn(f"subscribe {channel} ไม่ได้: {exc}")
    if not subscribed:
        client.close()
        fail("subscribe ไม่ได้สักช่อง — ตรวจ chatroom id อีกครั้ง")

    print(f"[OK]  ฟังแชทจากช่อง {subscribed} (กด Ctrl+C เพื่อหยุด)", file=sys.stderr)
    count = 0
    for name, data in client.events():
        msg = chat_message_to_dict(name, data)
        if msg:
            count += 1
            if args.json:
                print(json.dumps(msg, ensure_ascii=False))
            else:
                print(format_message(msg))
            if args.limit and count >= args.limit:
                break
        elif args.verbose:
            log(f"event: {name} -> {json.dumps(data, ensure_ascii=False)[:200]}")
    client.close()
    return 0


def cmd_listen(args) -> int:
    cfg = load_config(config_path(args.config))
    keys = app_keys_from(args, cfg)
    chatroom = args.chatroom or cfg.get("chatroom_id")

    if not chatroom and args.channel:
        log(f"ยังไม่รู้ chatroom id ของ {args.channel} — ลองดึงจาก kick.com ...")
        try:
            status, body = http_get(KICK_CHANNEL_API.format(slug=urllib.parse.quote(args.channel.strip().lstrip('@'))))
            if status == 200 and body.strip().startswith("{"):
                chatroom = (json.loads(body).get("chatroom") or {}).get("id")
        except (ConnectionError, json.JSONDecodeError):
            pass
        if chatroom:
            save_config(config_path(args.config), {"channel": args.channel, "chatroom_id": chatroom})
            ok(f"chatroom_id = {chatroom}")
    if not chatroom:
        fail(
            "ไม่รู้ chatroom id — ระบุด้วย --chatroom <id> หรือหาเองจาก\n"
            "      https://kick.com/<slug> (F12 > Console:\n"
            "      fetch('/api/v2/channels/<slug>').then(r=>r.json()).then(d=>console.log(d.chatroom.id)))"
        )

    channels = [f"chatrooms.{chatroom}.v2", f"chatrooms.{chatroom}", f"chatroom_{chatroom}"]
    configured_url = args.ws_url or cfg.get("ws_url")

    attempt = 0
    while True:
        attempt += 1
        url = configured_url or build_ws_url(keys[0])
        if configured_url and attempt > 1:
            url = configured_url
        try:
            return listen_session(url, channels, args)
        except KeyboardInterrupt:
            print(file=sys.stderr)
            log("หยุดโดยผู้ใช้")
            return 0
        except PusherWrongCluster as exc:
            warn(f"ต่อผิด cluster: {exc}")
            log("ลองหา cluster ที่ถูกให้อัตโนมัติ ...")
            for key in keys:
                for cluster in PROBE_ORDER:
                    if cluster == cfg.get("cluster"):
                        continue
                    status, detail = probe_once(build_ws_url(cluster, key), args.timeout)
                    if status == "ok":
                        ok(f"เจอ cluster ที่ใช้ได้: {cluster} (key {mask(key, 10)})")
                        save_config(config_path(args.config), {
                            "cluster": cluster, "app_key": key, "ws_url": build_ws_url(cluster, key),
                        })
                        configured_url = build_ws_url(cluster, key)
                        break
                else:
                    continue
                break
            else:
                fail("หา cluster ที่ใช้ได้ไม่เจอเลย — รัน: python kick_chat.py probe")
        except (PusherError, WebSocketError, OSError) as exc:
            warn(f"การเชื่อมต่อหลุด: {type(exc).__name__}: {exc}")
        if args.no_reconnect:
            return 1
        delay = min(args.max_backoff, 2 ** min(attempt, 6)) + random.uniform(0, 1.5)
        log(f"ลองใหม่ใน {delay:.1f} วินาที (ครั้งที่ {attempt + 1})")
        time.sleep(delay)


# ---------------------------------------------------------------------------
# คำสั่ง: send (API ทางการ)
# ---------------------------------------------------------------------------
def _oauth_module():
    try:
        sys.path.insert(0, str(SCRIPT_DIR))
        import kick_oauth  # type: ignore
        return kick_oauth
    except Exception as exc:  # pragma: no cover
        fail(f"โหลด kick_oauth.py ไม่ได้: {exc}")


def get_access_token(args) -> str:
    ko = _oauth_module()
    tokens_file = Path(args.tokens).expanduser() if args.tokens else SCRIPT_DIR / "kick_tokens.json"
    tokens = ko.load_tokens(tokens_file)
    if not tokens.get("access_token"):
        fail("ไม่พบ token — รัน:  python kick_oauth.py login")

    expires_at = tokens.get("expires_at")
    needs_refresh = (not expires_at) or (int(expires_at) - 60 < time.time())
    if needs_refresh and tokens.get("refresh_token") and not args.no_refresh:
        creds = Path(args.credentials).expanduser() if args.credentials else SCRIPT_DIR / "kick_credentials.json"
        settings = ko.load_settings(argparse.Namespace(
            credentials=str(creds) if creds.is_file() else None,
            tokens=str(tokens_file),
            redirect_uri=None, client_id=None, client_secret=None, scopes=None,
        ))
        if settings.get("client_id") and settings.get("client_secret"):
            log("access token ใกล้หมดอายุ — ขอใหม่จาก refresh token ...")
            try:
                resp = ko.http_post_form(ko.TOKEN_URL, {
                    "grant_type": "refresh_token",
                    "client_id": settings["client_id"],
                    "client_secret": settings["client_secret"],
                    "refresh_token": tokens["refresh_token"],
                })
                new_tokens = ko.tokens_from_response(resp)
                if new_tokens.get("access_token"):
                    if not new_tokens.get("refresh_token"):
                        new_tokens["refresh_token"] = tokens["refresh_token"]
                    ko.save_tokens(tokens_file, new_tokens)
                    ok("refresh token สำเร็จ")
                    return new_tokens["access_token"]
                warn("refresh ไม่สำเร็จ — ใช้ token เดิมต่อ")
            except SystemExit:
                warn("refresh ไม่สำเร็จ — ใช้ token เดิมต่อ")
    return tokens["access_token"]


def cmd_send(args) -> int:
    cfg = load_config(config_path(args.config))
    broadcaster = args.broadcaster_user_id or cfg.get("broadcaster_user_id")
    if args.type == "user" and not broadcaster:
        fail("โหมด user ต้องมี broadcaster_user_id (หาได้จาก: python kick_chat.py resolve --channel <slug>)")
    if len(args.message) > 500:
        fail(f"ข้อความยาวเกิน 500 ตัวอักษร (ตอนนี้ {len(args.message)})")

    token = get_access_token(args)
    payload: dict = {"type": args.type, "content": args.message}
    if args.type == "user":
        payload["broadcaster_user_id"] = int(broadcaster)
    if args.reply_to:
        payload["reply_to_message_id"] = args.reply_to

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/public/v1/chat",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", "replace")
            ok(f"ส่งข้อความสำเร็จ: {text.strip()}")
            return 0
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
        fail(f"ส่งไม่สำเร็จ HTTP {exc.code}: {detail.strip() or exc}")
    except urllib.error.URLError as exc:
        fail(f"เชื่อมต่อ api.kick.com ไม่ได้: {exc.reason}")


# ---------------------------------------------------------------------------
# คำสั่ง: doctor
# ---------------------------------------------------------------------------
def cmd_doctor(args) -> int:
    path = config_path(args.config)
    cfg = load_config(path)
    print("=== ตรวจสุขภาพระบบ Kick chat ===")
    print(f"      config file : {path} ({'พบ' if path.is_file() else 'ยังไม่มี'})")
    print(f"      cluster     : {cfg.get('cluster') or '(ยังไม่รู้ — จะทดสอบให้)'}")
    print(f"      app key     : {mask(cfg.get('app_key') or DEFAULT_APP_KEY, 12)}")
    print(f"      chatroom_id : {cfg.get('chatroom_id') or '(ยังไม่มี)'}")

    ko = _oauth_module()
    tokens_file = Path(args.tokens).expanduser() if args.tokens else SCRIPT_DIR / "kick_tokens.json"
    tokens = ko.load_tokens(tokens_file)
    print(f"      token file  : {tokens_file} ({'พบ' if tokens.get('access_token') else 'ไม่พบ token'})")
    if tokens.get("scope"):
        print(f"      scope       : {tokens['scope']}")
    print()

    if args.no_probe:
        return 0
    rc = cmd_probe(argparse.Namespace(
        config=args.config, app_key=args.app_key, clusters=args.clusters,
        timeout=args.timeout, all=False, ws_url=None,
    ))
    if rc != 0:
        return rc
    cfg = load_config(path)
    print()
    if cfg.get("chatroom_id"):
        print("พร้อมใช้งาน — เริ่มฟังแชทได้เลย:")
        print(f"  python kick_chat.py listen --chatroom {cfg['chatroom_id']}")
    else:
        print("ขั้นต่อไป: หา chatroom id ของช่องคุณ")
        print("  python kick_chat.py resolve --channel <ชื่อช่อง>")
    return 0


# ---------------------------------------------------------------------------
# วินิจฉัย + ซ่อม config ของผู้ใช้ (โครงสร้างไฟล์อิสระ)
# ---------------------------------------------------------------------------
KEY_FIELD_ALIASES = {
    "app_key": ("appkey", "pusherkey", "pusherappkey", "key", "appid"),
    "cluster": ("cluster", "pushercluster", "wscluster", "clustername"),
    "ws_url": ("wsurl", "wssurl", "websocketurl", "pusherurl", "wsendpoint", "endpoint"),
    "host": ("wshost", "host", "hostname", "pusherhost"),
}
# ชื่อฟิลด์ที่ยอมรับตอนเขียนกลับ (จะสร้างให้ถ้าไม่มี)
WRITE_FIELD_NAMES = {
    "cluster": ("cluster", "pusherCluster", "wsCluster"),
    "app_key": ("appKey", "app_key", "pusherKey", "key"),
    "ws_url": ("wsUrl", "ws_url", "websocketUrl", "pusherUrl"),
}

HOSTS_FILES = (
    Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts",
    Path("/etc/hosts"),
)


def resolve_host(host: str, port: int = 443) -> list[str]:
    """คืน IP ทั้งหมดที่ host นี้ resolve ได้ (เรียงตามที่ได้จากระบบ)"""
    ips: list[str] = []
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return ips
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


def is_private_ip(ip: str) -> bool:
    if ip.startswith(("127.", "10.", "192.168.", "169.254.", "::1", "fc", "fd", "fe80")):
        return True
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
            return 16 <= second <= 31
        except (IndexError, ValueError):
            return False
    return False


def hosts_file_hits(needle: str = "pusher") -> list[str]:
    hits: list[str] = []
    for path in HOSTS_FILES:
        try:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.split("#", 1)[0].strip()
                if stripped and needle in stripped.lower():
                    hits.append(f"{path}: {stripped}")
        except OSError:
            continue
    return hits


def proxy_env() -> dict:
    out = {}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        for candidate in (name, name.lower()):
            value = os.environ.get(candidate)
            if value:
                out[name] = value
    return out


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def walk_config(obj, path: str = "", depth: int = 0):
    """เดินดู JSON ทุกชั้น คืน (path, key, value)"""
    if depth > 6:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else str(key)
            if isinstance(value, (dict, list)):
                yield from walk_config(value, here, depth + 1)
            else:
                yield here, str(key), value
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from walk_config(value, f"{path}[{index}]", depth + 1)


def extract_pusher_settings(cfg: dict) -> dict:
    """หาค่า pusher จาก config ที่โครงสร้างไม่รู้ล่วงหน้า"""
    found: dict = {"paths": {}}
    for path, key, value in walk_config(cfg):
        if value in (None, "") or not isinstance(value, (str, int)):
            continue
        text = str(value).strip()
        nkey = _norm(key)
        if "ws_url" not in found and (text.startswith(("ws://", "wss://")) and "pusher" in text.lower()):
            found["ws_url"], found["paths"]["ws_url"] = text, path
            continue
        if "app_key" not in found and nkey in KEY_FIELD_ALIASES["app_key"] and re.fullmatch(r"[0-9a-fA-F]{12,40}", text):
            found["app_key"], found["paths"]["app_key"] = text, path
            continue
        if "cluster" not in found and nkey in KEY_FIELD_ALIASES["cluster"] and re.fullmatch(r"[a-z]{2,4}\d?", text.lower()):
            found["cluster"], found["paths"]["cluster"] = text.lower(), path
            continue
        if "host" not in found and nkey in KEY_FIELD_ALIASES["host"] and "pusher" in text.lower():
            found["host"], found["paths"]["host"] = text, path
    return found


def host_from_settings(settings: dict) -> str:
    if settings.get("ws_url"):
        return urllib.parse.urlparse(settings["ws_url"]).hostname or ""
    if settings.get("host"):
        return settings["host"]
    if settings.get("cluster"):
        return CLUSTER_HOSTS.get(settings["cluster"], f"ws-{settings['cluster']}.pusher.com")
    return ""


def _container_score(container: dict) -> int:
    """ให้คะแนนว่า dict ก้อนนี้ดูเป็นที่เก็บค่า pusher แค่ไหน"""
    score = 0
    norm = {_norm(k): v for k, v in container.items()}
    for field, aliases in KEY_FIELD_ALIASES.items():
        for alias in aliases:
            if alias not in norm:
                continue
            value = str(norm[alias]).strip()
            if field == "app_key" and re.fullmatch(r"[0-9a-fA-F]{12,40}", value):
                score += 3
            elif field == "ws_url" and value.startswith(("ws://", "wss://")):
                score += 3
            elif field == "host" and "pusher" in value.lower():
                score += 3
            elif field == "cluster" and re.fullmatch(r"[a-z]{2,4}\d?", value.lower()):
                score += 2
    return score


def patch_config_file(path: Path, values: dict, backup: bool = True) -> dict:
    """แก้ค่าใน config.json โดยคงคีย์อื่นทั้งหมดไว้ (คืน dict สรุปการเปลี่ยนแปลง)"""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("config ต้องเป็น JSON object")

    changes: dict = {}
    containers = [v for _, _, v in walk_config(data) if isinstance(v, dict)]
    containers.append(data)  # ตัวไฟล์เองเป็นตัวเลือกสุดท้าย

    # เลือก container ที่คะแนนสูงสุด (ถ้าไม่มีเลยให้สร้าง/ใช้บล็อก "pusher")
    scored = [(score, index, container) for index, container in enumerate(containers)
              if (score := _container_score(container)) > 0]
    if scored:
        target = max(scored, key=lambda item: (item[0], item[1]))[2]
    else:
        target = data.setdefault("pusher", {})
        if not isinstance(target, dict):
            raise ValueError('คีย์ "pusher" ในไฟล์ไม่ใช่ object')

    for field, value in values.items():
        if not value:
            continue
        wanted = WRITE_FIELD_NAMES.get(field, ())
        existing = next((k for k in target if _norm(k) in wanted), None)
        if existing is None:
            existing = {"cluster": "cluster", "app_key": "appKey", "ws_url": "wsUrl"}[field]
        if str(target.get(existing, "")) != str(value):
            changes[f"{existing}"] = {"old": target.get(existing), "new": value}
        target[existing] = value

    if changes:
        if backup:
            stamp_txt = time.strftime("%Y%m%d-%H%M%S")
            backup_path = path.with_suffix(path.suffix + f".bak-{stamp_txt}")
            backup_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            changes["_backup"] = str(backup_path)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return changes


def diagnose_verdict(
    *,
    configured_host: str,
    configured_key: str,
    live_key: str,
    live_cluster: str,
    handshake_ok: bool,
    handshake_error: str,
    resolved_ips: list[str],
    hosts_hits: list[str],
    proxies: dict,
) -> tuple[str, list[str]]:
    """คืน (สรุปผล, รายการที่ต้องแก้) — ฟังก์ชันบริสุทธิ์เพื่อทดสอบได้"""
    fixes: list[str] = []

    if hosts_hits:
        fixes.append("มีบรรทัด pusher ใน hosts file → ต้องลบ (ทำให้ traffic วิ่งไปที่อื่น)")
    if proxies:
        fixes.append(f"มี proxy env ตั้งอยู่ ({', '.join(proxies)}) → ปิดก่อนทดสอบ")
    if resolved_ips and all(is_private_ip(ip) for ip in resolved_ips):
        fixes.append(f"DNS ของ {configured_host} ชี้ไป IP วงใน ({', '.join(resolved_ips)}) → ถูกดักจับ/แก้ DNS")
    if configured_host and configured_host != "ws-us2.pusher.com":
        fixes.append(f"host ที่ตั้งไว้คือ {configured_host} — ต้องเป็น ws-us2.pusher.com")
    if live_key and configured_key and configured_key.lower() != live_key.lower():
        fixes.append(f"app key ในไฟล์ ({mask(configured_key, 10)}) ไม่ตรงกับ key ที่ Kick ใช้ตอนนี้ ({mask(live_key, 10)})")
    if live_cluster and live_cluster != "us2":
        fixes.append(f"Kick ย้ายไป cluster {live_cluster} แล้ว — ใช้ค่านั้นแทน us2")

    if handshake_ok:
        return ("เชื่อมต่อ Pusher ได้แล้ว — cluster/key ถูกต้อง", fixes)

    lowered = (handshake_error or "").lower()
    if "not in this cluster" in lowered:
        if configured_host and configured_host != "ws-us2.pusher.com":
            summary = f"ต่อผิด cluster (host {configured_host}) — key ถูก แต่ host ไม่ใช่ us2"
        elif live_key and configured_key and configured_key.lower() != live_key.lower():
            summary = "key ใน config ไม่ใช่ key ที่ Kick ใช้อยู่ตอนนี้ (Kick หมุน key)"
        else:
            summary = (
                "host ถูก (us2) และ key ตรงกับที่สาธารณะใช้ แต่ Pusher ยังปฏิเสธ "
                "→ เป็นได้ 2 อย่าง: (ก) Kick หมุน key ล่าสุด (ข) เส้นทางเน็ตถูกเปลี่ยน cluster (proxy/VPN/hosts)"
            )
            if not fixes:
                fixes.append("คัดลอก URL จริงจากเบราว์เซอร์ (Console snippet ในคู่มือ) แล้วรัน discover --ws-url")
    elif "does not exist" in lowered:
        summary = "app key ไม่ถูกต้องแล้ว (Kick เปลี่ยน key)"
        fixes.append("ไปเอา key จริงจากหน้าเว็บ Kick ด้วย Console snippet")
    else:
        summary = f"เชื่อมต่อไม่สำเร็จ: {handshake_error or 'ไม่ทราบสาเหตุ'}"
    return (summary, fixes)


def cmd_fix_config(args) -> int:
    path = Path(args.config_file).expanduser()
    if not path.is_file():
        fail(f"ไม่พบไฟล์: {path}")

    print(f"=== วินิจฉัย config: {path} ===")
    try:
        raw = path.read_text(encoding="utf-8-sig")
        cfg = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(f"ไฟล์ไม่ใช่ JSON ที่อ่านได้: {exc}")

    settings = extract_pusher_settings(cfg)
    configured_host = host_from_settings(settings)
    configured_key = str(settings.get("app_key") or "")
    print(f"      host ในไฟล์   : {configured_host or '(ไม่พบ)'}"
          + (f"   [จาก {settings['paths'].get('ws_url') or settings['paths'].get('cluster')}]" if configured_host else ""))
    print(f"      app key ในไฟล์ : {mask(configured_key, 10) if configured_key else '(ไม่พบ)'}"
          + (f"   [จาก {settings['paths'].get('app_key')}]" if configured_key else ""))
    if settings.get("ws_url"):
        print(f"      ws url ในไฟล์  : {settings['ws_url']}")

    # 1) ตรวจ hosts file / proxy / DNS
    print()
    print("--- ตรวจสิ่งรอบข้าง ---")
    hits = hosts_file_hits("pusher")
    if hits:
        warn("พบรายการ pusher ใน hosts file:")
        for hit in hits:
            print(f"      {hit}")
    else:
        ok("hosts file ไม่มีการแก้ไขที่เกี่ยวกับ pusher")

    proxies = proxy_env()
    if proxies:
        warn("พบ proxy env: " + ", ".join(f"{k}={v}" for k, v in proxies.items()))
    else:
        ok("ไม่มี proxy env (HTTP_PROXY/HTTPS_PROXY/ALL_PROXY)")

    for host in dict.fromkeys(h for h in (configured_host, "ws-us2.pusher.com") if h):
        ips = resolve_host(host)
        flag = "  <-- ผิดปกติ!" if ips and all(is_private_ip(ip) for ip in ips) else ""
        print(f"      DNS {host:<22} -> {', '.join(ips) if ips else '(resolve ไม่ได้)'}{flag}")

    # 2) หา key/cluster จริงจากเว็บ Kick (best-effort)
    print()
    print("--- หาค่าจริงจากเว็บ Kick (best-effort) ---")
    live_key, live_cluster = "", ""
    if args.ws_url:
        cluster, key, _host = parse_ws_url(args.ws_url)
        live_key, live_cluster = key, cluster or DEFAULT_CLUSTER
        ok(f"ใช้ค่าที่คุณวางมา: cluster={live_cluster} key={mask(live_key, 12)}")
    else:
        cluster, key, source = discover_from_web(args.channel)
        if key:
            live_key, live_cluster = key, cluster or DEFAULT_CLUSTER
            ok(f"เจอจาก {source}: cluster={live_cluster} key={mask(live_key, 12)}")
        else:
            warn("ดึงจากเว็บไม่เจอ (Cloudflare) — ใช้ snippet ในเบราว์เซอร์แล้วส่งมาทาง --ws-url")

    # 3) ยิง handshake จริง
    print()
    print("--- ทดสอบเชื่อมต่อจริง ---")
    test_key = configured_key or DEFAULT_APP_KEY
    test_host = configured_host or "ws-us2.pusher.com"
    url = settings.get("ws_url") or build_ws_url(settings.get("cluster") or DEFAULT_CLUSTER, test_key)
    status, detail = probe_once(url, args.timeout)
    print(f"      ลองต่อ {test_host} ด้วย key {mask(test_key, 10)} -> {status}: {detail}")
    if args.compare:
        compare_url = build_ws_url("eu", test_key)
        c_status, c_detail = probe_once(compare_url, args.timeout)
        print(f"      เทียบกับ ws-eu.pusher.com -> {c_status}: {c_detail}")
        if status == "wrong-cluster" and c_status == "wrong-cluster":
            print("      (ผลเหมือนกันทั้งสอง cluster = ปัญหาอยู่ที่ key/เส้นทาง ไม่ใช่แค่ตัว host)")

    summary, fixes = diagnose_verdict(
        configured_host=test_host,
        configured_key=test_key,
        live_key=live_key,
        live_cluster=live_cluster,
        handshake_ok=status == "ok",
        handshake_error=detail,
        resolved_ips=resolve_host(test_host),
        hosts_hits=hits,
        proxies=proxies,
    )
    print()
    print(f">>> สรุป: {summary}")
    if fixes:
        print("    ต้องแก้ / ต้องตรวจ:")
        for item in fixes:
            print(f"      - {item}")

    # 4) เสนอ/ลงมือแก้ config
    target_key = live_key or DEFAULT_APP_KEY
    target_cluster = live_cluster or DEFAULT_CLUSTER
    target_url = build_ws_url(target_cluster, target_key)
    values = {"cluster": target_cluster, "app_key": target_key, "ws_url": target_url}
    print()
    print("ค่าที่ควรเป็น:")
    print(json.dumps(values, ensure_ascii=False, indent=2))

    # ทดสอบค่าปลายทางจริง (ถ้าต่างจากที่ทดสอบไปแล้ว) เพื่อยืนยันว่าแก้แล้วจะใช้ได้
    target_status = status
    if target_url != url:
        target_status, target_detail = probe_once(target_url, args.timeout)
        print()
        print(f"--- ทดสอบค่าที่ควรเป็น ---")
        print(f"      ลองต่อ {host_from_settings(values) or target_cluster} ด้วย key {mask(target_key, 10)}"
              f" -> {target_status}: {target_detail}")

    if args.apply:
        try:
            changes = patch_config_file(path, values, backup=not args.no_backup)
        except (OSError, ValueError) as exc:
            fail(f"แก้ไฟล์ไม่สำเร็จ: {exc}")
        if changes:
            print()
            ok("แก้ไฟล์เรียบร้อย:")
            for field, change in changes.items():
                if field == "_backup":
                    print(f"      สำรองไว้ที่ : {change}")
                else:
                    print(f"      {field}: {mask(str(change['old']), 12)} -> {mask(str(change['new']), 12)}"
                          if field == "appKey" else f"      {field}: {change['old']} -> {change['new']}")
            print("      ทดสอบซ้ำด้วย:  python kick_chat.py listen --chatroom <id>")
        else:
            print()
            ok("ไฟล์มีค่าถูกต้องอยู่แล้ว ไม่ต้องแก้")
    else:
        print()
        info("ยังไม่แก้ไฟล์ — เพิ่ม --apply เพื่อให้แก้ให้ทันที (สำรอง .bak ไว้ให้)")

    if target_status == "ok":
        return 0
    if status == "ok":
        return 0
    return 1


# ---------------------------------------------------------------------------
# คำสั่ง: scan — หาทุกที่ในเครื่องที่มีค่า pusher (หา "ตัวที่ override" ให้เจอ)
# ---------------------------------------------------------------------------
SCAN_EXTENSIONS = {
    ".json", ".jsonc", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".env",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".txt", ".md", ".ps1", ".bat", ".sh",
}
SCAN_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".next"}

SCAN_PATTERNS = (
    ("ws-url", re.compile(r"wss?://[^\s\"'`,)]*pusher[^\s\"'`,)]*", re.I)),
    ("pusher-host", re.compile(r"\bws-[a-z0-9]+\.pusher\.com\b", re.I)),
    ("app-key", re.compile(r"(?:app_?key|pusher_?key|PUSHER_KEY)[\"'\s:=]+([0-9a-fA-F]{12,40})", re.I)),
    ("hex-key", re.compile(r"\b([0-9a-f]{32})\b")),
    ("cluster", re.compile(r"(?:cluster|PUSHER_CLUSTER)[\"'\s:=]+[\"']?([a-z]{2,4}\d?)", re.I)),
)


def scan_text_for_pusher(text: str) -> list[tuple[str, str]]:
    """คืน [(ชนิด, ข้อความที่เจอ)] แบบไม่ซ้ำ"""
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for kind, pattern in SCAN_PATTERNS:
        for match in pattern.finditer(text):
            value = (match.group(1) if match.groups() else match.group(0)).strip().strip("\"'` ")
            label = {"app-key": "appKey", "hex-key": "key", "cluster": "cluster",
                     "ws-url": "wsUrl", "pusher-host": "host"}[kind]
            found = f"{label} = {value}"
            key = (kind, value.lower())
            if key in seen:
                continue
            seen.add(key)
            hits.append((kind, found))
    return hits


def iter_scan_files(root: Path, exts: set[str], max_bytes: int = 2_000_000):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SCAN_SKIP_DIRS and not d.startswith(".")]
        for filename in filenames:
            path = Path(dirpath) / filename
            if path.suffix.lower() not in exts and filename.lower() not in (".env", "procfile"):
                continue
            try:
                if path.stat().st_size > max_bytes:
                    continue
            except OSError:
                continue
            yield path


def cmd_scan(args) -> int:
    root = Path(args.path).expanduser()
    if not root.exists():
        fail(f"ไม่พบ path: {root}")
    exts = {e if e.startswith(".") else "." + e for e in (args.exts or "").split(",") if e} or SCAN_EXTENSIONS

    print(f"=== สแกนหาค่า pusher ใน {root} ===")
    files = 0
    findings: list[tuple[str, int, str, str]] = []
    for path in iter_scan_files(root, exts):
        files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "pusher" not in text.lower() and "32cbd69e4b950bf97679" not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for kind, found in scan_text_for_pusher(line):
                findings.append((str(path), lineno, kind, found))

    if not findings:
        warn(f"สแกน {files} ไฟล์แล้วไม่พบค่า pusher เลย — ค่าอาจถูกตั้งจาก env หรือฝังในไฟล์ binary")
        return 1

    bad = 0
    for path, lineno, kind, found in findings:
        flag = ""
        if kind in ("ws-url", "pusher-host"):
            host = ""
            m = re.search(r"ws-[a-z0-9]+\.pusher\.com", found, re.I)
            if m:
                host = m.group(0).lower()
            if not m and "pusher" in found.lower():
                # ws URL ที่ไม่มี host ตามรูปแบบ (เช่นผ่าน proxy) — ไม่ตัดสิน
                pass
            if host and host != "ws-us2.pusher.com":
                flag = "   <-- ไม่ใช่ cluster ที่ Kick ใช้!"
                bad += 1
        if kind in ("app-key", "hex-key"):
            key = found.split("=", 1)[-1].strip()
            if key and key.lower() not in (DEFAULT_APP_KEY, *FALLBACK_APP_KEYS):
                flag = "   <-- key นี้ไม่ใช่ key ล่าสุดที่ Kick ใช้?"
                bad += 1
        print(f"{path}:{lineno}: [{kind}] {found}{flag}")

    print()
    print(f"      สแกน {files} ไฟล์ พบ {len(findings)} จุด" + (f" (น่าสงสัย {bad})" if bad else " (ดูปกติ)"))
    if bad:
        print()
        info("แก้ค่าที่ไม่ใช่ us2/" + DEFAULT_APP_KEY + " ให้เป็นค่าล่าสุด")
        info("หรือใช้:  python kick_chat.py fix-config --config-file \"<ไฟล์ที่มีค่าเก่า>\" --apply")
    return 0


# ---------------------------------------------------------------------------
# คำสั่ง: extract — แกะค่า pusher จาก "อะไรก็ได้" ที่ผู้ใช้วางมา
# ---------------------------------------------------------------------------
def best_pusher_guess(text: str) -> dict:
    """เลือกค่าที่น่าเชื่อถือที่สุดจากข้อความที่วางมา (แชท/ล็อก/ไฟล์/หน้าเว็บ)"""
    urls: list[str] = []
    for match in re.finditer(r"wss?://[^\s\"'`,)]+", text):
        url = match.group(0).rstrip(".,;")
        if "pusher" in url.lower() or re.search(r"/app/[0-9a-zA-Z]{8,}", url):
            if url not in urls:
                urls.append(url)

    keys: list[str] = []
    for match in re.finditer(r"\b([0-9a-fA-F]{32})\b", text):
        key = match.group(1)
        if key not in keys:
            keys.append(key)

    clusters: list[str] = []
    for match in re.finditer(r"\bws-([a-z0-9]+)\.pusher\.com\b", text, re.I):
        cluster = match.group(1).lower()
        if cluster not in clusters:
            clusters.append(cluster)
    for match in re.finditer(r"(?:cluster|PUSHER_CLUSTER)[\"'\s:=]+[\"']?([a-z]{2,4}\d?)", text, re.I):
        cluster = match.group(1).lower()
        if cluster in CLUSTER_HOSTS and cluster not in clusters:
            clusters.append(cluster)

    def url_score(url: str) -> tuple:
        # host สำคัญสุด: cluster ของ Kick คือ us2 — ถ้า log มีหลาย URL ให้เลือกตัวที่ชี้ us2
        parsed = urllib.parse.urlparse(url)
        key_in_url = re.search(r"/app/([0-9a-zA-Z]+)", parsed.path or "")
        key = key_in_url.group(1) if key_in_url else ""
        return (
            2 if "ws-us2" in (parsed.hostname or "") else 0,
            1 if "protocol=7" in url else 0,
            1 if key.lower() == DEFAULT_APP_KEY else 0,
        )

    best_url = max(urls, key=url_score) if urls else ""
    cluster = ""
    key = ""
    if best_url:
        cluster, key, _host = parse_ws_url(best_url)
    if not key:
        # ไม่เจอ key ในข้อความเลย → ใช้ key สาธารณะ (ค่าที่ Kick ใช้จริง)
        key = DEFAULT_APP_KEY if DEFAULT_APP_KEY in keys else (keys[0] if keys else DEFAULT_APP_KEY)
    if not cluster:
        cluster = "us2" if "us2" in clusters else (clusters[0] if clusters else DEFAULT_CLUSTER)

    return {
        "urls_found": urls,
        "keys_found": keys,
        "clusters_found": clusters,
        "best_url": best_url or build_ws_url(cluster, key),
        "cluster": cluster,
        "app_key": key,
    }


def canonical_pusher() -> dict:
    """ค่ามาตรฐานที่ Kick ใช้จริง (public key บน cluster us2)"""
    return {"cluster": "us2", "app_key": DEFAULT_APP_KEY, "ws_url": build_ws_url("us2", DEFAULT_APP_KEY)}


def cmd_extract(args) -> int:
    if args.text:
        text = args.text
        source = "argument --text"
    elif args.file:
        path = Path(args.file).expanduser()
        if not path.is_file():
            fail(f"ไม่พบไฟล์: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        source = str(path)
    else:
        if sys.stdin.isatty():
            print("วางข้อความที่ต้องการให้แกะค่า แล้วกด Ctrl+Z (Windows) หรือ Ctrl+D (Linux) แล้ว Enter:", file=sys.stderr)
        text = sys.stdin.read()
        source = "stdin"

    print(f"=== แกะค่า pusher จาก {source} ({len(text)} ตัวอักษร) ===")
    guess = best_pusher_guess(text)
    if not guess["urls_found"] and not guess["keys_found"] and not guess["clusters_found"]:
        warn("ไม่พบ URL/key/cluster ของ pusher ในข้อความนี้เลย")
        warn(f"ตัวอย่างที่ควรมี: wss://ws-us2.pusher.com/app/{DEFAULT_APP_KEY}?protocol=7")
        return 1

    print(f"      URL ที่เจอ    : {len(guess['urls_found'])}")
    for url in guess["urls_found"][:8]:
        print(f"        - {url}")
    if guess["keys_found"]:
        print(f"      key ที่เจอ    : {', '.join(mask(k, 10) for k in guess['keys_found'][:8])}")
    if guess["clusters_found"]:
        print(f"      cluster ที่เจอ: {', '.join(guess['clusters_found'])}")
    print()
    print("ค่าที่เลือกใช้:")
    print(json.dumps({
        "cluster": guess["cluster"],
        "app_key": guess["app_key"],
        "ws_url": guess["best_url"],
    }, ensure_ascii=False, indent=2))

    canonical = canonical_pusher()
    same_as_canonical = (guess["cluster"] == canonical["cluster"]
                         and guess["app_key"].lower() == canonical["app_key"].lower())
    if guess["app_key"] and guess["app_key"].lower() not in (DEFAULT_APP_KEY, *FALLBACK_APP_KEYS):
        warn(f"key ที่พบไม่ใช่ key สาธารณะที่รู้จัก ({mask(DEFAULT_APP_KEY, 10)}) — มักเกิดจากการเอา OAuth client_id มาใส่ช่อง appKey")
        print()
        print("ค่ามาตรฐานที่ Kick ใช้จริง (ถ้าใช้ค่านี้จะต่อได้แน่):")
        print(json.dumps(canonical, ensure_ascii=False, indent=2))

    chosen = {"cluster": guess["cluster"], "app_key": guess["app_key"], "ws_url": guess["best_url"]}
    if args.probe:
        print()
        print("--- ทดสอบเชื่อมต่อ ---")
        status, detail = probe_once(chosen["ws_url"], args.timeout)
        print(f"      ค่าจากข้อความ  -> {status}: {detail}")
        if not same_as_canonical:
            c_status, c_detail = probe_once(canonical["ws_url"], args.timeout)
            print(f"      ค่ามาตรฐาน us2 -> {c_status}: {c_detail}")
            if c_status == "ok" and status != "ok":
                ok("ค่ามาตรฐานใช้ได้ (ค่าจากข้อความใช้ไม่ได้) — จะใช้ค่ามาตรฐานในการแก้ไฟล์")
                chosen = canonical
        elif status != "ok":
            warn("ค่ามาตรฐานก็ยังต่อไม่ได้ — น่าจะเป็นปัญหาเส้นทาง/เน็ต (ดูคำสั่ง report)")

    if args.config_file:
        if args.apply:
            try:
                changes = patch_config_file(Path(args.config_file).expanduser(), {
                    "cluster": chosen["cluster"], "app_key": chosen["app_key"], "ws_url": chosen["ws_url"],
                }, backup=not args.no_backup)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                fail(f"แก้ไฟล์ไม่สำเร็จ: {exc}")
            if changes:
                ok(f"แก้ไฟล์แล้ว: {args.config_file}")
                print(f"      สำรอง: {changes.get('_backup', '(ไม่ได้สำรอง)')}")
            else:
                ok("ไฟล์มีค่าตรงกันอยู่แล้ว")
        else:
            info(f"เพิ่ม --apply เพื่อเขียนค่าลง {args.config_file} (จะสำรอง .bak ให้)")
    return 0


# ---------------------------------------------------------------------------
# คำสั่ง: report — รวมข้อมูลวินิจฉัยเป็นบล็อกเดียวสำหรับส่งต่อ
# ---------------------------------------------------------------------------
def _run_cmd(cmd: list[str], timeout: float = 15.0) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


def python_processes() -> str:
    """รายการ process python ที่กำลังรัน (ช่วยเผย process เก่าที่ค้างอยู่)"""
    if os.name == "nt":
        parts = []
        for image in ("python.exe", "pythonw.exe"):
            out = _run_cmd(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"])
            if out and "no tasks" not in out.lower():
                parts.append(out)
        detail = _run_cmd([
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | "
            "Select-Object ProcessId,CreationDate,CommandLine | Format-List",
        ])
        if not detail:
            detail = _run_cmd([
                "wmic", "process", "where", "name='python.exe'",
                "get", "ProcessId,CreationDate,CommandLine", "/format:list",
            ])
        if detail:
            parts.append(detail)
        return "\n".join(parts) or "(ไม่พบ process python)"
    out = _run_cmd(["ps", "-eo", "pid,ppid,etime,cmd"])
    return _filter_ps_output(out, os.getpid())


def _filter_ps_output(out: str, own_pid: int) -> str:
    """กรอง ps output ให้เหลือแต่ process python ที่ไม่ใช่ตัวสคริปต์เอง/เชลล์ที่ห่อคำสั่ง"""
    lines: list[str] = []
    for line in out.splitlines():
        if "python" not in line.lower():
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid, cmd = parts[0], parts[3]
        if pid == str(own_pid):
            continue
        if cmd.startswith(("/bin/sh", "/bin/bash", "bash -", "sh -", "ps -eo")):
            continue
        if "kick_chat.py report" in cmd:
            continue
        lines.append(line)
    return "\n".join(lines) or "(ไม่พบ process python อื่นนอกจากตัวสคริปต์เอง)"


def collect_secrets() -> list[str]:
    """รวบรวมค่าลับที่มีในเครื่อง เพื่อลบออกจากรายงานก่อนแสดง"""
    secrets: list[str] = []
    for name in ("kick_credentials.json", "kick_credentials.env", "credentials.json", ".env"):
        for base in (SCRIPT_DIR, Path.cwd()):
            path = base / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for match in re.finditer(r"(?:client_?secret|secret)[\"'\s:=]+([^\s\"',}]+)", text, re.I):
                value = match.group(1).strip()
                if len(value) >= 8:
                    secrets.append(value)
    for base in (SCRIPT_DIR, Path.cwd()):
        path = base / "kick_tokens.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            for field in ("client_secret", "access_token", "refresh_token"):
                value = data.get(field)
                if isinstance(value, str) and len(value) >= 8:
                    secrets.append(value)
    return list(dict.fromkeys(secrets))


def redact(text: str, secrets: list[str], mask_keys: bool = True) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***REDACTED***")
    if mask_keys:
        text = re.sub(r"\b([0-9a-fA-F]{32})\b", lambda m: mask(m.group(1), 8), text)
    return text


def cmd_report(args) -> int:
    secrets = collect_secrets()
    lines: list[str] = []
    add = lines.append

    add("=== KICK CHAT REPORT (คัดลอกทั้งบล็อกนี้ส่งกลับมาได้เลย) ===")
    add(f"เวลา        : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    add(f"ระบบ        : {sys.platform} / Python {sys.version.split()[0]}")
    add(f"โฟลเดอร์     : {SCRIPT_DIR}")

    add("")
    add("--- process python ที่กำลังรัน ---")
    add(python_processes())

    add("")
    add("--- config ---")
    path = config_path(args.config)
    add(f"ไฟล์ config  : {path} ({'พบ' if path.is_file() else 'ไม่พบ'})")
    cfg = load_config(path)
    settings = extract_pusher_settings(cfg) if cfg else {}
    if settings:
        add(f"ค่าที่อ่านได้  : {json.dumps({k: v for k, v in settings.items() if k != 'paths'}, ensure_ascii=False)}")
        add(f"มาจากฟิลด์    : {json.dumps(settings.get('paths', {}), ensure_ascii=False)}")
    else:
        add("ค่าที่อ่านได้  : (ไม่พบค่า pusher ในไฟล์)")

    add("")
    add("--- token (ไม่ใช้กับ Pusher — ใช้เฉพาะตอนส่งข้อความ) ---")
    tokens_file = Path(args.tokens).expanduser() if args.tokens else SCRIPT_DIR / "kick_tokens.json"
    ko = _oauth_module()
    tokens = ko.load_tokens(tokens_file)
    if tokens.get("access_token"):
        add(f"ไฟล์ token   : {tokens_file} (พบ)")
        add(f"scope       : {tokens.get('scope') or '(ไม่ระบุ)'}")
        add(f"อายุ         : {ko.token_expiry_text(tokens)}")
        add(f"มี refresh   : {'มี' if tokens.get('refresh_token') else 'ไม่มี'}")
    else:
        add(f"ไฟล์ token   : {tokens_file} (ไม่พบ token — ต้องรัน kick_oauth.py login)")

    add("")
    add("--- สิ่งที่อาจดัก/แก้เส้นทาง ---")
    hits = hosts_file_hits("pusher")
    add(f"hosts file   : {hits if hits else 'ไม่มีการแก้ที่เกี่ยวกับ pusher'}")
    proxies = proxy_env()
    add(f"proxy env    : {proxies if proxies else 'ไม่มี'}")
    candidates: list[str] = []
    if settings:
        candidates.append(host_from_settings(settings))
    candidates += ["ws-us2.pusher.com", "ws-eu.pusher.com"]
    for host in dict.fromkeys(h for h in candidates if h):
        ips = resolve_host(host)
        flag = "  <-- IP วงใน (ผิดปกติ)" if ips and all(is_private_ip(ip) for ip in ips) else ""
        add(f"DNS {host:<20}: {', '.join(ips) if ips else '(resolve ไม่ได้)'}{flag}")

    add("")
    add("--- ทดสอบเชื่อมต่อ Pusher จริง (ดูข้อความ error ว่ามันบอก key อะไร) ---")
    tests: list[tuple[str, str]] = []
    configured_key = str(settings.get("app_key") or "") if settings else ""
    configured_cluster = str(settings.get("cluster") or "") if settings else ""
    if settings.get("ws_url"):
        tests.append(("ตาม config (wsUrl)", settings["ws_url"]))
    if configured_key or configured_cluster:
        tests.append((
            f"ตาม config (cluster={configured_cluster or 'us2'})",
            build_ws_url(configured_cluster or DEFAULT_CLUSTER, configured_key or DEFAULT_APP_KEY),
        ))
    tests.append(("มาตรฐาน us2 + key สาธารณะ", build_ws_url("us2", DEFAULT_APP_KEY)))
    if not args.fast:
        tests.append(("เทียบ ws-eu + key สาธารณะ (control)", build_ws_url("eu", DEFAULT_APP_KEY)))

    for label, url in tests:
        parsed = urllib.parse.urlparse(url)
        key_match = re.search(r"/app/([0-9a-zA-Z]+)", parsed.path or "")
        key_shown = mask(key_match.group(1), 8) if key_match else "?"
        try:
            status, detail = probe_once(url, args.timeout)
        except SystemExit:
            status, detail = "error", "(โปรแกรมออกกลางทาง)"
        add(f"{label}:")
        add(f"   {parsed.hostname} key {key_shown} -> {status}: {detail}")
        if key_match and key_match.group(1).lower() not in (DEFAULT_APP_KEY, *FALLBACK_APP_KEYS):
            add(f"   (หมายเหตุ: key ข้างบนไม่ใช่ key สาธารณะ {DEFAULT_APP_KEY})")
    add("")
    add("ตรวจให้ครบ 3 อย่างในข้อความ error: (1) key ที่ Pusher พูดถึง (2) host ที่ต่อ (3) ใครเป็นคนรัน")
    add("=== จบบล็อกรายงาน ===")

    print(redact("\n".join(lines), secrets, mask_keys=not args.show_keys))
    warn("รายงานนี้ปิดบัง client secret / token ให้แล้ว (key ถูกย่อเหลือ 8 ตัวแรก)")
    warn("อย่าใช้ --show-keys แล้วส่งต่อในที่สาธารณะ")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", metavar="FILE", help=f"ไฟล์ config (ค่าเริ่มต้น {CONFIG_NAME})")
    common.add_argument("--app-key", dest="app_key", help="Pusher app key (ค่าเริ่มต้นคือ key สาธารณะของ Kick)")
    common.add_argument("--clusters", help="ระบุ cluster ที่จะทดสอบ เช่น us2,eu")
    common.add_argument("--timeout", type=float, default=12.0, help="หมดเวลา (วินาที) ต่อการเชื่อมต่อ 1 ครั้ง")
    common.add_argument("--ws-url", dest="ws_url", help="ใช้ URL ที่คัดลอกมาจาก DevTools ตรง ๆ")

    parser = argparse.ArgumentParser(
        prog="kick_chat.py",
        description="Kick chat (Pusher WebSocket) — วินิจฉัย cluster, ซ่อม config, ฟังแชท, ส่งข้อความ",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", metavar="{probe,discover,resolve,listen,send,doctor,fix-config,scan,extract,report}")

    p = sub.add_parser("probe", parents=[common], help="ทดสอบทุก cluster ว่าอันไหนใช้ได้")
    p.add_argument("--all", action="store_true", help="ทดสอบทุก cluster (ไม่หยุดที่ตัวแรก)")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("discover", parents=[common], help="ดึง wss URL จากเว็บ Kick หรือจาก URL ที่วางมา")
    p.add_argument("--channel", help="ชื่อช่อง (slug) เพื่อเจาะหน้าเว็บนั้นก่อน")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("resolve", parents=[common], help="หา channel_id / chatroom_id จาก slug")
    p.add_argument("--channel", required=True, help="ชื่อช่อง เช่น yourslug")
    p.add_argument("--chatroom", help="ระบุ chatroom id เองถ้าดึงอัตโนมัติไม่ได้")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("listen", parents=[common], help="ต่อ WebSocket แล้วอ่านแชทสด")
    p.add_argument("--channel", help="ชื่อช่อง (slug) ใช้หา chatroom id")
    p.add_argument("--chatroom", help="chatroom id (ถ้ารู้อยู่แล้ว)")
    p.add_argument("--json", action="store_true", help="ส่งออกเป็น JSON 1 บรรทัด/ข้อความ (สำหรับ pipe)")
    p.add_argument("--limit", type=int, default=0, help="หยุดหลังได้ครบ N ข้อความ (0 = ไม่หยุด)")
    p.add_argument("--verbose", action="store_true", help="แสดง event อื่น ๆ ด้วย")
    p.add_argument("--no-reconnect", action="store_true", help="ไม่ต้อง reconnect เมื่อหลุด")
    p.add_argument("--max-backoff", type=float, default=60.0, help="ดีเลย์สูงสุดตอน reconnect (วินาที)")
    p.set_defaults(func=cmd_listen)

    p = sub.add_parser("send", parents=[common], help="ส่งข้อความเข้าแชท (API ทางการ)")
    p.add_argument("--message", required=True, help="ข้อความ (ไม่เกิน 500 ตัวอักษร)")
    p.add_argument("--channel", help="ชื่อช่อง (บันทึกไว้ใช้ครั้งหน้า)")
    p.add_argument("--broadcaster-user-id", dest="broadcaster_user_id", type=int, help="user id ของเจ้าของช่อง")
    p.add_argument("--type", choices=["user", "bot"], default="user", help="ส่งในนาม user หรือ bot")
    p.add_argument("--reply-to", dest="reply_to", help="message_id ที่ต้องการตอบกลับ")
    p.add_argument("--tokens", help="ไฟล์ token (ค่าเริ่มต้น kick_tokens.json)")
    p.add_argument("--credentials", help="ไฟล์ credentials (ค่าเริ่มต้น kick_credentials.json)")
    p.add_argument("--no-refresh", action="store_true", help="ไม่ต้อง refresh token อัตโนมัติ")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("doctor", parents=[common], help="ตรวจทุกอย่างรวบรัด (ค่าเริ่มต้น)")
    p.add_argument("--tokens", help="ไฟล์ token (ค่าเริ่มต้น kick_tokens.json)")
    p.add_argument("--no-probe", action="store_true", help="ข้ามการทดสอบ cluster")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser(
        "fix-config", parents=[common],
        help="วินิจฉัย config.json ของคุณ (hosts file/proxy/DNS) แล้วซ่อมค่า pusher ให้",
    )
    p.add_argument("--config-file", dest="config_file", required=True, help="path ของ config.json ที่ต้องการตรวจ/ซ่อม")
    p.add_argument("--channel", help="ชื่อช่อง (slug) สำหรับดึงค่าจริงจากเว็บ Kick")
    p.add_argument("--apply", action="store_true", help="ลงมือแก้ไฟล์จริง (มีสำรอง .bak)")
    p.add_argument("--no-backup", action="store_true", help="ไม่ต้องสำรองไฟล์ก่อนแก้")
    p.add_argument("--no-compare", dest="compare", action="store_false", default=True,
                   help="ไม่ต้องเทียบกับ ws-eu")
    p.set_defaults(func=cmd_fix_config)

    p = sub.add_parser("scan", parents=[common],
                       help="สแกนหาทุกที่ในโฟลเดอร์ที่มีค่า pusher (URL/key/cluster)")
    p.add_argument("--path", required=True, help="โฟลเดอร์ที่ต้องการสแกน เช่น D:\\HermesAI\\OBS")
    p.add_argument("--exts", help="นามสกุลไฟล์ที่สนใจ คั่นด้วยจุลภาค (ค่าเริ่มต้น: รายการที่พบทั่วไป)")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("extract", parents=[common],
                       help="วางข้อความ/ล็อก/URL อะไรก็ได้ แล้วให้แกะค่า pusher (กันคัดลอกผิดช่อง)")
    p.add_argument("--text", help="ข้อความที่ต้องการแกะ")
    p.add_argument("--file", help="ไฟล์ที่ต้องการแกะ (เช่นไฟล์ .txt ที่วาง URL ไว้)")
    p.add_argument("--probe", action="store_true", help="ทดสอบเชื่อมต่อค่าที่แกะได้")
    p.add_argument("--config-file", dest="config_file", help="ไฟล์ config ที่ต้องการให้แก้")
    p.add_argument("--apply", action="store_true", help="เขียนค่าลง config file จริง")
    p.add_argument("--no-backup", action="store_true", help="ไม่ต้องสำรองไฟล์ก่อนแก้")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("report", parents=[common],
                       help="รวมรายงานวินิจฉัย (process/config/token/hosts/DNS/probe) เป็นบล็อกเดียว")
    p.add_argument("--tokens", help="ไฟล์ token (ค่าเริ่มต้น kick_tokens.json)")
    p.add_argument("--fast", action="store_true", help="ไม่ต้องทดสอบ cluster เทียบ (เร็วขึ้น)")
    p.add_argument("--show-keys", dest="show_keys", action="store_true",
                   help="แสดง key เต็มในรายงาน (อย่าส่งต่อที่สาธารณะ)")
    p.set_defaults(func=cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"probe", "discover", "resolve", "listen", "send", "doctor", "fix-config", "scan", "extract", "report"}
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["doctor"] + argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        log("ยกเลิกโดยผู้ใช้ (Ctrl+C)")
        return 130
    except PusherWrongCluster as exc:
        warn(f"ต่อผิด cluster: {exc}")
        print("      แก้ด้วย:  python kick_chat.py probe", file=sys.stderr)
        return 1
    except (PusherError, WebSocketError) as exc:
        fail(f"{type(exc).__name__}: {exc}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(130)
