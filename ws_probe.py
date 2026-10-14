#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ws_probe.py — ตรวจ "เส้นทางจริง" ไปยัง Pusher ของ Kick แบบไฟล์เดียว 0 dependency

ตอบคำถามเดียว: ทำไม Pusher ถึงตอบ "App key ... not in this cluster"

สำคัญ (ต.ค. 2026): Kick ปิด Pusher app ตัวเก่า `32cbd69e4b950bf97679` แล้ว
ทุกการเชื่อมต่อด้วย key นั้นจะได้ `4001 App key not in this cluster`
key ที่ใช้ได้ตอนนี้คือ `34bf7a0ff419a2a775b9` (cluster us2) และ Kick มี endpoint
ให้ถามได้เสมอว่า "ตอนนี้ใช้ app ไหน": POST https://web.kick.com/api/v1/realtime/connection

ใช้:
  python ws_probe.py                                  # ทดสอบค่ามาตรฐานที่ Kick ใช้ตอนนี้
  python ws_probe.py --discover                       # ถาม Kick ตรง ๆ แล้วใช้ key/cluster ที่ได้
  python ws_probe.py --url "wss://ws-us2.pusher.com/app/34bf...?protocol=7&..."
  python ws_probe.py --key 34bf7a0ff419a2a775b9 --cluster us2
  python ws_probe.py --from-config "D:\\HermesAI\\OBS\\files\\config.json"
  python ws_probe.py --repeat 2                       # ยิงซ้ำเพื่อดูว่า DNS เปลี่ยน/ไม่คงที่ไหม

สิ่งที่ตรวจ:
  1) DNS  → IP ที่ได้ (mark ถ้าเป็น IP วงใน = ถูกดัก)
  2) TCP  → เชื่อมต่อได้ไหม ใช้เวลากี่ ms
  3) TLS  → เวอร์ชัน/cipher + "ใครออกใบรับรอง" (ถ้าไม่ใช่ CA สาธารณะ = มีคนถอดรหัสกลางทาง)
  4) Handshake → HTTP 101? (ถ้าไม่ได้ จะบอกว่า proxy อะไรตอบกลับมา)
  5) ข้อความแรกจาก Pusher → ok หรือ error พร้อม "key ที่ Pusher เห็น"
  6) ตรวจ key ที่จะส่ง → มีอักขระซ่อน (zero-width/ช่องว่าง) หรือไม่
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import socket
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

# key ที่ Kick ใช้อยู่จริงตอนนี้ (ค่าที่ Kick คืนจาก web.kick.com/api/v1/realtime/connection วันที่ 13 ต.ค. 2026)
DEFAULT_KEY = "34bf7a0ff419a2a775b9"
# key เก่าที่ Kick ปิดไปแล้ว (ต.ค. 2026) — เจอเมื่อไรให้เตือนเสมอ
RETIRED_KEYS = ("32cbd69e4b950bf97679",)
DEFAULT_CLUSTER = "us2"
HOST_TEMPLATE = "ws-{cluster}.pusher.com"
KNOWN_KEYS = (DEFAULT_KEY,)
KEY_RE = re.compile(r"^[0-9a-f]{12,40}$", re.I)
# endpoint ที่ Kick ใช้บอกว่า "ตอนนี้เชื่อมต่อ provider ไหน ด้วย app อะไร"
REALTIME_CONNECTION_URL = "https://web.kick.com/api/v1/realtime/connection"
PUSHER_VERSION = "8.4.0"


def out(msg: str = "") -> None:
    print(msg, flush=True)


def _fix_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[attr-defined]
        except Exception:
            pass


# ---------------------------------------------------------------------------
def key_report(key: str) -> list[str]:
    """ตรวจ key ว่ามีปัญหาอะไร (คืนรายการคำเตือน)"""
    issues: list[str] = []
    if key != key.strip():
        issues.append("มีช่องว่าง/อักขระ whitespace ต้นหรือท้าย")
    weird = [(i, ch, f"U+{ord(ch):04X}") for i, ch in enumerate(key) if not ("0" <= ch <= "9" or "a" <= ch.lower() <= "f")]
    for i, _ch, code in weird[:6]:
        name = {
            "U+200B": "ZERO WIDTH SPACE",
            "U+200C": "ZERO WIDTH NON-JOINER",
            "U+200D": "ZERO WIDTH JOINER",
            "U+FEFF": "BOM / ZERO WIDTH NO-BREAK SPACE",
            "U+00A0": "NO-BREAK SPACE",
            "U+2028": "LINE SEPARATOR",
            "U+005C": "BACKSLASH — อาจเป็น \\u200b ที่ยังไม่ถูกถอด",
        }.get(code, "อักขระที่ไม่ใช่ hex")
        issues.append(f"ตำแหน่ง {i}: {code} ({name})")
    if not KEY_RE.match(key):
        issues.append(f"รูปแบบไม่ใช่ hex 12-40 ตัว (ตอนนี้ยาว {len(key)})")
    if key.lower() in RETIRED_KEYS:
        issues.append(
            f"key นี้ถูก Kick ปิดไปแล้ว (ต.ค. 2026) — จะได้ error 4001 'not in this cluster' เสมอ; "
            f"ใช้ {DEFAULT_KEY} แทน (หรือรัน --discover เพื่อถาม Kick)"
        )
    elif key.lower() not in KNOWN_KEYS:
        issues.append(
            f"ไม่ใช่ key สาธารณะที่รู้จักตอนนี้ ({DEFAULT_KEY}) — "
            "ถ้าเป็นค่าจาก Developer Portal นั่นคือ OAuth client_id ซึ่งใช้กับ Pusher ไม่ได้"
        )
    return issues


def print_key_check(key: str) -> None:
    out("=== ตรวจ key ที่จะส่ง ===")
    out(f"   repr() : {key!r}")
    out(f"   ยาว    : {len(key)} ตัวอักษร")
    issues = key_report(key)
    if not issues:
        out("   >>> ปรกติ (hex ล้วน, ยาวถูก)")
    else:
        for issue in issues:
            out(f"   [!!] {issue}")
    out()


# ---------------------------------------------------------------------------
def parse_realtime_connection(payload) -> tuple[str, str] | None:
    """ดึง (key, cluster) จากคำตอบของ web.kick.com/api/v1/realtime/connection

    รูปแบบ: {"data": {"connections": [{"provider": "pusher",
              "credentials": {"app_key": "...", "cluster": "us2"}}]}}
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    connections = data.get("connections") if isinstance(data, dict) else None
    if not isinstance(connections, list):
        return None
    for item in connections:
        if not isinstance(item, dict):
            continue
        if str(item.get("provider", "")).lower() != "pusher":
            continue
        creds = item.get("credentials")
        if not isinstance(creds, dict):
            continue
        key = str(creds.get("app_key") or "").strip()
        cluster = str(creds.get("cluster") or DEFAULT_CLUSTER).strip() or DEFAULT_CLUSTER
        if key and KEY_RE.match(key):
            return key, cluster
    return None


def fetch_official_connection(timeout: float = 10.0) -> tuple[tuple[str, str] | None, str]:
    """ถาม Kick ว่าจะให้เชื่อมต่อ Pusher app ไหน (คืน ((key, cluster) หรือ None, ข้อความอธิบาย)"""
    import urllib.request

    body = json.dumps({
        "client": {"id": str(uuid.uuid4()), "type": "web"},
        "capabilities": {"accepted_providers": [{"provider": "pusher"}]},
    }).encode()
    request = urllib.request.Request(
        REALTIME_CONNECTION_URL,
        data=body,
        headers={
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ws_probe.py",
            "origin": "https://kick.com",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return None, f"Kick ตอบ HTTP {exc.code} (ถ้า 403 = Cloudflare บล็อกเครื่องนี้ ให้ใช้ค่าเริ่มต้น {DEFAULT_KEY})"
    except Exception as exc:  # noqa: BLE001 — รายงานทุกความล้มเหลวให้เห็น
        return None, f"ติดต่อ web.kick.com ไม่ได้: {type(exc).__name__}: {exc}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, "คำตอบไม่ใช่ JSON (อาจโดน Cloudflare กั้น)"
    found = parse_realtime_connection(payload)
    if not found:
        return None, "คำตอบไม่มีข้อมูล pusher/credentials"
    return found, "ok"


# ---------------------------------------------------------------------------
def _unescape(value: str) -> str:
    """แปลง \\u200b (escape ในไฟล์ JSON) ให้เป็นอักขระจริง เพื่อจับอักขระซ่อนได้"""
    if "\\" not in value:
        return value
    try:
        return json.loads('"' + value.replace('"', '\\"') + '"')
    except json.JSONDecodeError:
        return value


def _walk_config(obj, depth: int = 0):
    if depth > 5:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key), value
            yield from _walk_config(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_config(value, depth + 1)


def extract_from_config(path: str) -> tuple[str, str]:
    """ดึง (key, url) จากไฟล์ config: อ่านเป็น JSON ก่อน ถ้าไม่ได้ค่อยใช้ regex"""
    try:
        text = open(path, encoding="utf-8-sig", errors="replace").read()
    except OSError as exc:
        out(f"[!] อ่านไฟล์ไม่ได้: {exc}")
        return "", ""

    url = ""
    key = ""

    # 1) ลองอ่านเป็น JSON ก่อน (แม่นสุด — ได้ค่าจริงหลังถอด escape)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None

    if isinstance(data, (dict, list)):
        for name, value in _walk_config(data):
            if not isinstance(value, str):
                continue
            if not url and value.startswith(("ws://", "wss://")):
                url = value.strip()
            if not key and re.sub(r"[^a-z0-9]", "", name.lower()) in (
                "appkey", "pusherkey", "app_key", "pusher_key", "key",
            ) and len(value.strip()) >= 12:
                key = value.strip()
        if not key and url:
            m = re.search(r"/app/([0-9a-zA-Z]+)", url)
            if m:
                key = m.group(1)

    # 2) ไม่ใช่ JSON → ใช้ regex บนข้อความดิบ
    if not url:
        m = re.search(r"wss?://[^\s\"'`,)]+", text)
        if m:
            url = m.group(0).rstrip(".,;")
    if not key:
        for pattern in (
            r"\"(?:appKey|app_key|pusherKey|key)\"\s*:\s*\"([^\"]+)\"",
            r"(?:appKey|app_key|PUSHER_KEY|pusher_key)\s*[=:]\s*['\"]?([0-9a-zA-Z]{12,40})",
        ):
            m = re.search(pattern, text, re.I)
            if m:
                key = _unescape(m.group(1))
                break
    if not key and url:
        m = re.search(r"/app/([0-9a-zA-Z]+)", url)
        if m:
            key = m.group(1)
    if not key:
        m = re.search(r"\b([0-9a-f]{32})\b", text, re.I)
        if m:
            key = m.group(1)

    key = _unescape(key)
    if url:
        parsed = urllib.parse.urlparse(url)
        out(f"   อ่านจาก config: host={parsed.hostname or '?'} "
            f"key={key[:8] + '...' if len(key) > 8 else key}")
    return key, url


def is_private_ip(ip: str) -> bool:
    if ip.startswith(("127.", "10.", "192.168.", "169.254.", "::1", "fe80")):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def resolve(host: str, port: int = 443) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        out(f"   [!!] resolve ไม่ได้: {exc}")
        return []
    ips: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    return ips


# ---------------------------------------------------------------------------
def read_http_headers(sock: socket.socket, timeout: float) -> tuple[str, dict[str, str], bytes]:
    buf = b""
    deadline = time.time() + timeout
    while b"\r\n\r\n" not in buf:
        if time.time() > deadline:
            raise TimeoutError("หมดเวลารอ header")
        sock.settimeout(max(0.5, deadline - time.time()))
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("เซิร์ฟเวอร์ปิดระหว่างอ่าน header")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", "replace").split("\r\n")
    status = lines[0]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip():
            headers[name.strip()] = value.strip()
    return status, headers, rest


class WSReader:
    """อ่าน WebSocket frame โดยใช้บัฟเฟอร์ที่เหลือจาก handshake ก่อน (สำคัญมาก: Pusher
    ส่ง frame แรกมาพร้อมกับ HTTP 101 response ได้เลย)"""

    def __init__(self, sock: socket.socket, leftover: bytes = b""):
        self.sock = sock
        self.buf = bytearray(leftover or b"")

    def _recv_into(self, timeout: float) -> None:
        self.sock.settimeout(timeout)
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("ปิดการเชื่อมต่อ")
        self.buf += chunk

    def read_exact(self, n: int, timeout: float) -> bytes:
        while len(self.buf) < n:
            self._recv_into(timeout)
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def next_frame(self, timeout: float) -> str | None:
        head = self.read_exact(2, timeout)
        opcode = head[0] & 0x0F
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", self.read_exact(2, timeout))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.read_exact(8, timeout))[0]
        payload = self.read_exact(length, timeout) if length else b""
        if opcode == 0x8:
            code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 1005
            return f"(close frame: code {code} {payload[2:120]!r})"
        if opcode not in (0x1, 0x2, 0x0):
            return None
        return payload.decode("utf-8", "replace")


def probe(url: str, timeout: float, show_frames: int = 3) -> int:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme in ("wss", "https") else 80)
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")

    out(f"=== ทดสอบ: {host}:{port} ===")
    key_match = re.search(r"/app/([0-9a-zA-Z]+)", parsed.path or "")
    key = key_match.group(1) if key_match else ""
    if key:
        print_key_check(key)
        if key.strip().lower() in RETIRED_KEYS:
            out(f"[!!] key นี้อยู่ในรายการที่ Kick ปิดแล้ว — ผลที่ได้จะเป็นการยืนยันว่าเก่า ไม่ใช่ปัญหาเครื่องคุณ")
            out()

    # 1) DNS
    t0 = time.time()
    ips = resolve(host, port)
    dns_ms = (time.time() - t0) * 1000
    if not ips:
        out(">>> หยุด: DNS ใช้ไม่ได้ (ตรวจเน็ต/DNS หรือ hosts file)")
        return 2
    flag = "   <-- IP วงใน (ถูกดัก!)" if all(is_private_ip(ip) for ip in ips) else ""
    out(f"1) DNS    : {', '.join(ips)}  ({dns_ms:.0f} ms){flag}")

    # 2) TCP
    t0 = time.time()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        out(f"2) TCP    : [!!] เชื่อมต่อไม่ได้: {type(exc).__name__}: {exc}")
        out(">>> หยุด: ไฟร์วอลล์/เน็ตบล็อกพอร์ต 443 (หรือ proxy บังคับให้ใช้พอร์ตอื่น)")
        return 3
    out(f"2) TCP    : ต่อได้ใน {(time.time() - t0) * 1000:.0f} ms")

    # 3) TLS
    if parsed.scheme in ("wss", "https"):
        try:
            ctx = ssl.create_default_context()
            tls = ctx.wrap_socket(sock, server_hostname=host)
        except ssl.SSLError as exc:
            out(f"3) TLS    : [!!] {type(exc).__name__}: {exc}")
            out(">>> มีคนกลางทางสกัด TLS — ถ้าเช็คไม่ได้ว่าใคร ลองปิด security software/VPN")
            try:
                sock.close()
            except OSError:
                pass
            return 4
        cert = tls.getpeercert() or {}
        version = tls.version() or "?"
        cipher = (tls.cipher() or ("?",))[0]
        subject = ", ".join(f"{k}={v}" for item in cert.get("subject", ()) for k, v in item)
        issuer = ", ".join(f"{k}={v}" for item in cert.get("issuer", ()) for k, v in item)
        san = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
        out(f"3) TLS    : {version} / {cipher}")
        out(f"   cert   : {subject or '(ไม่ทราบ)'}")
        out(f"   ออกโดย : {issuer or '(ไม่ทราบ)'}")
        out(f"   ใช้ได้  : {cert.get('notBefore')} .. {cert.get('notAfter')}")
        out(f"   SAN    : {', '.join(san) if san else '(ไม่มี)'}")
        if not any("pusher" in name for name in san):
            out("   [!!] ใบรับรองนี้ไม่ได้ออกให้ *.pusher.com → มี proxy/security software ถอดรหัสกลางทางแน่นอน")
        elif "DigiCert" not in issuer and "Let's Encrypt" not in issuer and "Amazon" not in issuer and "Google" not in issuer:
            out(f"   [!]  ผู้ออกไม่ใช่ CA สาธารณะที่คุ้นเคย — ตรวจสอบ (อาจเป็น MITM ขององค์กร)")
        sock = tls

    # 4) WebSocket handshake
    ws_key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {ws_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Origin: https://kick.com\r\n"
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) ws_probe.py\r\n\r\n"
    )
    sock.sendall(request.encode())
    try:
        status, headers, leftover = read_http_headers(sock, timeout)
    except (TimeoutError, ConnectionError, OSError) as exc:
        out(f"4) WS     : [!!] {type(exc).__name__}: {exc}")
        out(">>> สงสัย: proxy/ไฟร์วอลล์ตัดการเชื่อมต่อหลัง TLS (WebSocket ถูกบล็อก)")
        try:
            sock.close()
        except OSError:
            pass
        return 5
    out(f"4) WS     : {status}")
    for name in ("Server", "Via", "X-Cache", "CF-Ray", "X-Forwarded-For"):
        if name in headers:
            out(f"   {name}: {headers[name]}")

    if "101" not in status:
        out(">>> ไม่ได้ 101 = ไม่ได้คุยกับ Pusher จริง — ข้อความด้านบนคือคนที่ตอบกลับมาจากกลางทาง")
        try:
            sock.close()
        except OSError:
            pass
        return 6

    # 5) อ่านข้อความแรกจาก Pusher (ใช้ข้อมูลที่ค้างในบัฟเฟอร์จาก handshake ด้วย)
    reader = WSReader(sock, leftover)
    frames: list[str] = []
    for _ in range(max(1, show_frames)):
        try:
            frame = reader.next_frame(timeout)
        except (TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
            out(f"5) Frame  : [!!] อ่านไม่ได้: {type(exc).__name__}: {exc}")
            break
        if frame is None:
            continue
        frames.append(frame)
        event = "?"
        message = ""
        try:
            data = json.loads(frame)
            event = data.get("event", "?")
            inner = data.get("data")
            if isinstance(inner, str):
                try:
                    inner = json.loads(inner)
                except json.JSONDecodeError:
                    pass
            if isinstance(inner, dict):
                message = str(inner.get("message") or "")
        except json.JSONDecodeError:
            pass
        out(f"5) Frame  : event={event}")
        if message:
            out(f"   ข้อความจาก Pusher: {message}")
        if event in ("pusher:connection_established", "pusher:error"):
            break

    print()
    ok = any('"pusher:connection_established"' in frame or "pusher:connection_established" in frame for frame in frames)
    if ok:
        out(">>> ✅ ต่อได้ปกติ — host + key ที่ส่งไปถูกต้องทั้งคู่")
        out("    ถ้าโปรแกรมของคุณยัง error แปลว่ามันส่งค่าอื่น (คนละ key/host) หรือยังไม่ได้รีสตาร์ท")
    else:
        for frame in frames:
            if "not in this cluster" in frame:
                m = re.search(r"App key\s+(\S+?)\s+not in this cluster", frame)
                seen = m.group(1) if m else "?"
                out(f">>> ❌ Pusher บอกว่ามันเห็น key: {seen!r}")
                if seen.strip().lower() == (key or "").strip().lower():
                    out("    key ที่คุณส่ง = key ที่ Pusher เห็น → ถ้า key นี้ไม่ใช่ key ปัจจุบันของ Kick ให้เปลี่ยนเป็น " + DEFAULT_KEY)
                else:
                    out("    ⚠ key ที่ Pusher เห็น ไม่ตรงกับ key ที่คุณคิดว่าส่งไป!")
                    out("    นี่คือหลักฐานว่าโค้ดของคุณใช้ค่าอื่นตอนรัน (อ่านฟิลด์ผิด/env ทับ/hardcode)")
                break
        else:
            if frames:
                out(">>> ❓ ได้ข้อความจาก Pusher แต่ไม่ใช่ทั้งสองแบบที่รู้จัก — ส่งบรรทัดด้านบนมาให้ดู")
            else:
                out(">>> ❓ เชื่อมต่อได้แต่ Pusher ไม่ส่งข้อความอะไรกลับมาเลย")
    try:
        sock.close()
    except OSError:
        pass
    return 0 if ok else 7


# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    _fix_stdout()
    parser = argparse.ArgumentParser(
        prog="ws_probe.py",
        description="ตรวจเส้นทางไป Pusher ของ Kick (DNS/TCP/TLS/WebSocket/key) — 0 dependency",
    )
    parser.add_argument("--url", help="URL เต็ม เช่น wss://ws-us2.pusher.com/app/<key>?protocol=7&...")
    parser.add_argument("--key", help=f"app key (ค่าเริ่มต้น {DEFAULT_KEY})")
    parser.add_argument("--cluster", help=f"cluster (ค่าเริ่มต้น {DEFAULT_CLUSTER})")
    parser.add_argument("--from-config", dest="from_config", help="อ่าน key/url จากไฟล์ config (JSON หรือข้อความ)")
    parser.add_argument("--discover", action="store_true",
                        help="ถาม Kick ตรง ๆ ว่าใช้ Pusher app ไหน แล้วทดสอบค่านั้น")
    parser.add_argument("--timeout", type=float, default=12.0, help="หมดเวลา (วินาที)")
    parser.add_argument("--repeat", type=int, default=1, help="ยิงซ้ำกี่ครั้ง (ดู DNS/เส้นทางไม่คงที่)")
    args = parser.parse_args(argv)

    out("=" * 68)
    out("ws_probe.py — ตรวจว่าปัญหาอยู่ที่ 'เส้นทาง' หรือที่ 'key/โค้ดของเรา'")
    out("=" * 68)

    url = args.url or ""
    key = args.key or ""

    if args.discover:
        out("=== ถาม Kick: ตอนนี้ให้เชื่อมต่อ Pusher app ไหน ===")
        found, reason = fetch_official_connection()
        if found:
            key, cluster = found
            url = (
                f"wss://{HOST_TEMPLATE.format(cluster=cluster)}/app/{key}"
                f"?protocol=7&client=js&version={PUSHER_VERSION}&flash=false"
            )
            out(f"[OK]  Kick บอกให้ใช้: key={key} · cluster={cluster}")
            out("      (ใช้ค่านี้ได้เลย — ไม่ต้องเดาอีก)")
        else:
            out(f"[!!]  ถามไม่ได้: {reason}")
            if key.strip().lower() in RETIRED_KEYS or not key:
                key = DEFAULT_KEY
            out(f"      ใช้ค่าเริ่มต้นในไฟล์นี้แทน: {key} · cluster {args.cluster or DEFAULT_CLUSTER}")
        out()

    if args.from_config:
        out(f"=== อ่านค่าจาก {args.from_config} ===")
        cfg_key, cfg_url = extract_from_config(args.from_config)
        url = url or cfg_url
        key = key or cfg_key
        out()

    if not url:
        cluster = args.cluster or DEFAULT_CLUSTER
        key = key or DEFAULT_KEY
        url = (
            f"wss://{HOST_TEMPLATE.format(cluster=cluster)}/app/{key}"
            f"?protocol=7&client=js&version={PUSHER_VERSION}&flash=false"
        )

    if not key:
        m = re.search(r"/app/([0-9a-zA-Z]+)", urllib.parse.urlparse(url).path or "")
        key = m.group(1) if m else ""

    rc = 0
    for attempt in range(1, max(1, args.repeat) + 1):
        if args.repeat > 1:
            out(f"--- ครั้งที่ {attempt}/{args.repeat} ---")
        rc = probe(url, args.timeout)
        if attempt < args.repeat:
            out()
    out()
    out(f"(exit code {rc}: 0 = ต่อได้, 2 DNS, 3 TCP, 4 TLS, 5 อ่าน header ไม่ได้, 6 ไม่ได้ 101, 7 Pusher ปฏิเสธ)")
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
