#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kick_events.py — ใช้ Events API (webhook) ของ Kick แทน Pusher WebSocket

ทำไมใช้ตัวนี้:
  - เป็น API ทางการ มีเอกสารรับรอง ไม่ต้องพึ่ง Pusher app key/cluster ที่เปลี่ยนได้เงียบ ๆ
  - รับ event ได้ครบ: chat.message.sent, channel.followed, channel.subscription.*, moderation.banned, kicks.gifted, ...
  - ต้องมี URL แบบ HTTPS ที่ Kick ยิงเข้ามาได้ (ดูวิธีทำ tunnel ท้ายไฟล์นี้ / ใน kick-chat-fix.md)

คำสั่ง:
  subscribe  ลงทะเบียน webhook ของคุณกับ Kick  (ต้องมี token สโคป events:subscribe)
  list       ดู subscription ที่มีอยู่
  delete     ยกเลิก subscription ตาม id
  serve      เปิดเซิร์ฟเวอร์รับ webhook ในเครื่อง (ตรวจลายเซ็น RSA ให้อัตโนมัติ)
  verify     ทดสอบไฟล์/ข้อความตัวอย่างว่าลายเซ็นถูกต้องไหม

ตัวอย่าง:
  python kick_events.py subscribe --webhook https://abc.trycloudflare.com/kick --broadcaster 123456
  python kick_events.py serve --port 8081 --path /kick --write chat.ndjson
  python kick_events.py list
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.server
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.kick.com"
SUBSCRIPTIONS_URL = API_BASE + "/public/v1/events/subscriptions"
PUBLIC_KEY_URL = API_BASE + "/public/v1/public-key"
DEFAULT_EVENTS = "chat.message.sent"
SCRIPT_DIR = Path(__file__).resolve().parent

# PKCS#1 v1.5 DigestInfo prefix สำหรับ SHA-256 (RFC 8017)
SHA256_DIGEST_INFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _fix_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[attr-defined]
        except Exception:
            pass


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def fail(msg: str, code: int = 1):
    print(f"[X]   {msg}", file=sys.stderr)
    raise SystemExit(code)


def mask(value: str | None, keep: int = 8) -> str:
    if not value:
        return "(ว่าง)"
    return value[:keep] + "..." if len(value) > keep else value


# ---------------------------------------------------------------------------
# token (อ่าน/รีเฟรชเอง ; ใช้ไฟล์เดียวกับ kick_oauth.py)
# ---------------------------------------------------------------------------
def load_tokens(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_tokens(path: Path, tokens: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(tokens)
    payload["saved_at"] = int(time.time())
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_credentials(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    if text.lstrip().startswith("{"):
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    creds: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_]+)\s*[:=]\s*(.*)$", line)
        if m:
            creds[m.group(1).upper()] = m.group(2).strip().strip("\"'")
    return {
        "client_id": creds.get("KICK_CLIENT_ID"),
        "client_secret": creds.get("KICK_CLIENT_SECRET"),
    }


def tokens_file(args) -> Path:
    if getattr(args, "tokens", None):
        return Path(args.tokens).expanduser()
    return SCRIPT_DIR / "kick_tokens.json"


def credentials_file(args) -> Path:
    if getattr(args, "credentials", None):
        return Path(args.credentials).expanduser()
    return SCRIPT_DIR / "kick_credentials.json"


def refresh_access_token(tokens: dict, creds: dict) -> dict:
    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")
    if not (client_id and client_secret and tokens.get("refresh_token")):
        return {}
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": tokens["refresh_token"],
    }).encode()
    req = urllib.request.Request(
        "https://id.kick.com/oauth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as exc:
        log(f"refresh token ไม่สำเร็จ: {exc}")
        return {}
    new = {
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token") or tokens.get("refresh_token"),
        "token_type": data.get("token_type", "Bearer"),
        "scope": data.get("scope", tokens.get("scope", "")),
    }
    if data.get("expires_in"):
        new["expires_in"] = int(data["expires_in"])
        new["expires_at"] = int(time.time()) + int(data["expires_in"])
    return new


def get_access_token(args, auto_refresh: bool = True) -> str:
    path = tokens_file(args)
    tokens = load_tokens(path)
    if not tokens.get("access_token"):
        fail("ไม่พบ token — รัน:  python kick_oauth.py login")
    expires_at = tokens.get("expires_at")
    if auto_refresh and (not expires_at or int(expires_at) - 60 < time.time()) and tokens.get("refresh_token"):
        log("access token ใกล้/หมดอายุ — กำลังขอใหม่ ...")
        refreshed = refresh_access_token(tokens, load_credentials(credentials_file(args)))
        if refreshed.get("access_token"):
            save_tokens(path, refreshed)
            log("refresh สำเร็จ")
            return refreshed["access_token"]
        log("refresh ไม่ได้ — ใช้ token เดิมต่อ (ถ้า 401 ให้ login ใหม่)")
    return tokens["access_token"]


# ---------------------------------------------------------------------------
# HTTP helpers (API ของ Kick)
# ---------------------------------------------------------------------------
def api_request(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, dict | str]:
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(text) if text.strip() else {}
            except json.JSONDecodeError:
                return resp.status, text
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace") if exc.fp else ""
        try:
            return exc.code, json.loads(text) if text.strip() else {}
        except json.JSONDecodeError:
            return exc.code, text
    except urllib.error.URLError as exc:
        fail(f"เชื่อมต่อ {urllib.parse.urlparse(url).netloc} ไม่ได้: {exc.reason}")


# ---------------------------------------------------------------------------
# ตรวจลายเซ็น RSA (pure Python, ไม่ต้องติดตั้งอะไร)
# ---------------------------------------------------------------------------
def _der_parse(data: bytes, pos: int = 0) -> tuple[int, bytes, int]:
    """คืน (tag, content, ตำแหน่งถัดไป) — parser แบบพอใช้สำหรับโครงสร้าง DER ที่เกี่ยวข้อง"""
    if pos + 2 > len(data):
        raise ValueError("DER สั้นเกินไป")
    tag = data[pos]
    pos += 1
    length = data[pos]
    pos += 1
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or pos + count > len(data):
            raise ValueError("ความยาว DER ไม่ถูกต้อง")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    if pos + length > len(data):
        raise ValueError("เนื้อหา DER ยาวเกินข้อมูล")
    return tag, data[pos:pos + length], pos + length


def parse_rsa_public_key_pem(pem: str) -> tuple[int, int]:
    """แยก (modulus n, exponent e) จาก PEM แบบ 'PUBLIC KEY' (PKIX SubjectPublicKeyInfo)"""
    text = pem.strip()
    if "-----BEGIN" not in text:
        raise ValueError("ไม่พบ PEM ในข้อมูลที่ให้")
    b64 = "".join(
        line.strip() for line in text.splitlines()
        if line.strip() and "-----" not in line
    )
    der = base64.b64decode(b64)
    tag, spki, _ = _der_parse(der)
    if tag != 0x30:
        raise ValueError("โครงสร้าง SPKI ไม่ถูกต้อง")
    pos = 0
    _tag, _alg, pos = _der_parse(spki, pos)          # AlgorithmIdentifier
    tag, bitstring, _ = _der_parse(spki, pos)
    if tag != 0x03:
        raise ValueError("ไม่พบ BIT STRING ของกุญแจ")
    inner = bitstring[1:]                            # ตัดไบต์ 'unused bits'
    tag, rsa_seq, _ = _der_parse(inner)
    if tag != 0x30:
        raise ValueError("ไม่พบโครงสร้าง RSAPublicKey")
    tag_n, n_bytes, pos = _der_parse(rsa_seq, 0)
    tag_e, e_bytes, _ = _der_parse(rsa_seq, pos)
    if tag_n != 0x02 or tag_e != 0x02:
        raise ValueError("RSAPublicKey ต้องเป็น INTEGER สองตัว")
    n = int.from_bytes(n_bytes, "big")
    e = int.from_bytes(e_bytes, "big")
    if n <= 0 or e <= 0:
        raise ValueError("ค่ากุญแจไม่ถูกต้อง")
    return n, e


def rsa_verify_sha256(n: int, e: int, signature: bytes, message: bytes) -> bool:
    """ตรวจลายเซ็น RSASSA-PKCS1-v1_5 + SHA-256 (RFC 8017)"""
    k = (n.bit_length() + 7) // 8
    if len(signature) != k:
        return False
    m = pow(int.from_bytes(signature, "big"), e, n)
    em = m.to_bytes(k, "big")
    if not em.startswith(b"\x00\x01"):
        return False
    idx = em.find(b"\x00", 2)
    if idx < 3:  # ต้องมี FF อย่างน้อย 8 ไบต์ตามมาตรฐาน
        return False
    if any(byte != 0xFF for byte in em[2:idx]):
        return False
    tail = em[idx + 1:]
    expected = SHA256_DIGEST_INFO_PREFIX + hashlib.sha256(message).digest()
    return hmac.compare_digest(tail, expected)


def build_signed_message(message_id: str, timestamp: str, body: bytes) -> bytes:
    """สตริงที่ Kick ใช้เซ็น: '<message_id>.<timestamp>.<raw body>'"""
    return f"{message_id}.{timestamp}.".encode("utf-8") + body


def fetch_public_key(url: str = PUBLIC_KEY_URL, token: str = "") -> tuple[str, tuple[int, int]]:
    """ดึงกุญแจสาธารณะของ Kick (คืน PEM + (n, e)) — ห้าม hardcode เพราะ Kick หมุนกุญแจได้"""
    headers = {"Accept": "application/json, text/plain"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise ConnectionError(f"ดึง public key ไม่ได้: {exc}") from exc

    pem = ""
    if "-----BEGIN" in text:
        pem = text
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError("รูปแบบ public key ที่ได้ไม่รู้จัก")
        for candidate in _walk_values(data):
            if isinstance(candidate, str) and "-----BEGIN" in candidate:
                pem = candidate
                break
    if not pem:
        raise ValueError("ไม่พบ PEM ในคำตอบของ public-key")
    return pem, parse_rsa_public_key_pem(pem)


def _walk_values(obj, depth: int = 0):
    if depth > 4:
        return
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _walk_values(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_values(value, depth + 1)
    else:
        yield obj


# ---------------------------------------------------------------------------
# แปลง payload event -> ข้อความอ่านง่าย
# ---------------------------------------------------------------------------
def summarize_event(event_type: str, payload: dict) -> str:
    def who(person: dict | None) -> str:
        person = person or {}
        return str(person.get("username") or person.get("user_id") or "?")

    if event_type == "chat.message.sent":
        sender = payload.get("sender") or {}
        badges = [b.get("type") for b in ((sender.get("identity") or {}).get("badges") or []) if isinstance(b, dict)]
        badge_txt = f"({','.join(filter(None, badges))}) " if badges else ""
        content = payload.get("content") or ""
        return f"[แชท] {badge_txt}{who(sender)}: {content}"
    if event_type == "channel.followed":
        return f"[ติดตาม] {who(payload.get('follower'))} ติดตามช่องแล้ว"
    if event_type.startswith("channel.subscription"):
        return f"[ซับ] {who(payload.get('subscriber') or payload.get('gifter'))} — {event_type}"
    if event_type == "moderation.banned":
        return f"[แบน] {who(payload.get('user'))} ถูกแบน (โดย {who(payload.get('moderator'))})"
    if event_type == "kicks.gifted":
        return f"[KICKs] {who(payload.get('gifter'))} ส่ง {payload.get('amount', '?')} KICKs"
    if event_type.startswith("livestream."):
        return f"[ไลฟ์] {event_type} — {json.dumps(payload, ensure_ascii=False)[:160]}"
    return f"[{event_type}] {json.dumps(payload, ensure_ascii=False)[:200]}"


def normalize_chat(payload: dict, event_type: str = "chat.message.sent") -> dict:
    """ทำให้ payload เหมือนกับที่ Pusher ให้ เพื่อให้โค้ดปลายทางใช้ร่วมกันได้"""
    sender = payload.get("sender") or {}
    identity = sender.get("identity") or {}
    badges = []
    for badge in identity.get("badges") or []:
        if isinstance(badge, dict):
            badges.append(str(badge.get("type") or badge.get("text") or ""))
    return {
        "id": payload.get("message_id"),
        "created_at": payload.get("created_at"),
        "user_id": sender.get("user_id"),
        "username": sender.get("username"),
        "slug": sender.get("channel_slug"),
        "color": identity.get("username_color"),
        "badges": badges,
        "content": payload.get("content"),
        "type": event_type,
        "raw": payload,
    }


# ---------------------------------------------------------------------------
# webhook receiver
# ---------------------------------------------------------------------------
class WebhookHandler(http.server.BaseHTTPRequestHandler):
    server_version = "KickEvents/1.0"

    def do_POST(self):  # noqa: N802
        cfg = self.server.cfg  # type: ignore[attr-defined]
        parsed = urllib.parse.urlparse(self.path)
        if cfg["path"] and parsed.path.rstrip("/") != cfg["path"].rstrip("/"):
            self.send_error(404, "path not matched")
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""

        message_id = self.headers.get("Kick-Event-Message-Id", "")
        timestamp = self.headers.get("Kick-Event-Message-Timestamp", "")
        signature_b64 = self.headers.get("Kick-Event-Signature", "")
        event_type = self.headers.get("Kick-Event-Type", "")

        verified: bool | None = None
        reason = ""
        if cfg["verify"]:
            if not (message_id and timestamp and signature_b64):
                verified, reason = False, "ขาด header ที่ต้องใช้ตรวจลายเซ็น"
            else:
                try:
                    signature = base64.b64decode(signature_b64, validate=True)
                except (ValueError, base64.binascii.Error):
                    verified, reason = False, "Kick-Event-Signature ไม่ใช่ base64"
                else:
                    n, e = cfg["public_key"]
                    signed = build_signed_message(message_id, timestamp, body)
                    if rsa_verify_sha256(n, e, signature, signed):
                        verified = True
                    else:
                        verified, reason = False, "ลายเซ็นไม่ตรงกับ public key ของ Kick"

        payload: dict = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                payload = {"_raw": body.decode("utf-8", "replace")[:500]}

        record = {
            "id": message_id,
            "event": event_type,
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "verified": verified,
            "payload": payload,
        }

        if verified is True or verified is None:
            cfg["count"] += 1
            if event_type == "chat.message.sent" or cfg["all_events"]:
                line = summarize_event(event_type, payload)
                print(line, flush=True)
            if cfg["json_lines"] and event_type == "chat.message.sent":
                print(json.dumps(normalize_chat(payload, event_type), ensure_ascii=False), flush=True)
            if cfg["write"]:
                with cfg["write_lock"]:
                    with open(cfg["write"], "a", encoding="utf-8") as handle:
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            if cfg["forward"]:
                threading.Thread(target=cfg["forward"], args=(body, self.headers, event_type), daemon=True).start()
            if cfg["limit"] and cfg["count"] >= cfg["limit"]:
                threading.Thread(target=self.server.shutdown, daemon=True).start()  # type: ignore[attr-defined]
        else:
            log(f"ปฏิเสธ event: {reason} (event={event_type or '?'})")

        status = 200
        if verified is False and not cfg["accept_bad"]:
            status = 401
        response = json.dumps({"ok": status == 200, "verified": verified}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def do_GET(self):  # noqa: N802
        # เปิดหน้าเว็บไว้ให้ health-check / tunnel ตรวจว่า endpoint มีชีวิต
        cfg = self.server.cfg  # type: ignore[attr-defined]
        body = json.dumps({
            "service": "kick_events receiver",
            "path": cfg["path"],
            "received": cfg["count"],
            "verify": cfg["verify"],
        }, ensure_ascii=False, indent=2).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # ลดเสียงรบกวน
        pass


def make_forwarder(url: str):
    def forward(body: bytes, headers, event_type: str) -> None:
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "Kick-Event-Type": event_type},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                log(f"forward -> {url}: HTTP {resp.status}")
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            log(f"forward ไม่สำเร็จ: {exc}")
    return forward


# ---------------------------------------------------------------------------
# คำสั่ง
# ---------------------------------------------------------------------------
def cmd_subscribe(args) -> int:
    if not args.webhook.lower().startswith("https://"):
        fail("webhook ต้องเป็น https:// เท่านั้น (Kick ไม่รับ http://)")
    token = get_access_token(args)
    events = [e.strip() for e in (args.events or DEFAULT_EVENTS).split(",") if e.strip()]
    payload = {
        "events": [{"name": name, "version": 1} for name in events],
        "method": "webhook",
        "broadcaster_user_id": int(args.broadcaster),
    }
    if args.webhook:
        payload["url"] = args.webhook
    log(f"ลงทะเบียน {len(events)} event ไปที่ {args.webhook} ...")
    status, data = api_request("POST", SUBSCRIPTIONS_URL, token, payload)
    if status in (200, 201):
        print(json.dumps(data, ensure_ascii=False, indent=2))
        print()
        print("[OK]  ลงทะเบียนสำเร็จ — ตรวจว่า endpoint ของคุณตอบ 200 เร็ว ๆ เสมอ")
        print("      Kick จะเลิกรับส่งถ้า webhook ล้มเหลวต่อเนื่องเกิน 1 วัน")
        return 0
    if status in (401, 403):
        warn = "token ไม่มีสิทธิ์ events:subscribe หรือหมดอายุ"
    elif status == 400:
        warn = "คำขอไม่ถูกต้อง (ตรวจ broadcaster_user_id / url / event name)"
    else:
        warn = "โปรดอ่านข้อความ error ด้านบน"
    print(json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, dict) else str(data))
    fail(f"ลงทะเบียนไม่สำเร็จ (HTTP {status}) — {warn}")
    return 1


def cmd_list(args) -> int:
    token = get_access_token(args)
    url = SUBSCRIPTIONS_URL + (f"?broadcaster_user_id={args.broadcaster}" if args.broadcaster else "")
    status, data = api_request("GET", url, token)
    if status != 200:
        print(json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, dict) else str(data))
        fail(f"ดูรายการไม่สำเร็จ (HTTP {status})")
    items = data.get("data") if isinstance(data, dict) else data
    if not items:
        print("(ยังไม่มี subscription)")
        return 0
    print(f"{'id':<30} {'event':<32} {'method':<9} broadcaster")
    for item in items:
        print(f"{str(item.get('id')):<30} {str(item.get('event')):<32} "
              f"{str(item.get('method')):<9} {item.get('broadcaster_user_id')}")
    print(f"\nทั้งหมด {len(items)} รายการ")
    return 0


def cmd_delete(args) -> int:
    token = get_access_token(args)
    url = SUBSCRIPTIONS_URL + "?" + urllib.parse.urlencode([("id", i) for i in args.id])
    status, data = api_request("DELETE", url, token)
    if status in (200, 204):
        print(f"[OK]  ยกเลิกแล้ว {len(args.id)} รายการ")
        return 0
    print(json.dumps(data, ensure_ascii=False, indent=2) if isinstance(data, dict) else str(data))
    fail(f"ยกเลิกไม่สำเร็จ (HTTP {status})")
    return 1


def cmd_serve(args) -> int:
    try:
        # public-key เป็น endpoint สาธารณะ ไม่ต้องใช้ token
        pem, key = fetch_public_key(args.public_key_url)
    except (ConnectionError, ValueError) as exc:
        if args.verify:
            fail(f"{exc}\n      ใช้ --no-verify ถ้าต้องการทดสอบก่อน (ไม่แนะนำให้ใช้ในงานจริง)")
        pem, key = "", (0, 0)
    if args.verify:
        log(f"ดึง public key สำเร็จ ({(key[0].bit_length())} บิต)")
        if args.save_key:
            Path(args.save_key).write_text(pem, encoding="utf-8")
            log(f"บันทึกกุญแจไว้ที่ {args.save_key}")

    cfg = {
        "path": args.path,
        "verify": args.verify,
        "accept_bad": args.accept_bad,
        "all_events": args.all_events,
        "json_lines": args.json_lines,
        "write": args.write,
        "write_lock": threading.Lock(),
        "forward": make_forwarder(args.forward) if args.forward else None,
        "public_key": key,
        "count": 0,
        "limit": args.limit,
    }
    bind = args.bind or "0.0.0.0"
    try:
        server = http.server.ThreadingHTTPServer((bind, args.port), WebhookHandler)
    except OSError as exc:
        fail(f"เปิดพอร์ต {args.port} ไม่ได้: {exc}")
    server.cfg = cfg  # type: ignore[attr-defined]

    print(f"[OK]  รับ webhook ที่  http://{bind}:{args.port}{args.path}  (กด Ctrl+C เพื่อหยุด)", flush=True)
    if args.verify:
        print("      ตรวจลายเซ็น RSA (Kick-Event-Signature) ให้อัตโนมัติ — event ที่ลายเซ็นผิดจะได้ 401", flush=True)
    else:
        print("      [!] ปิดการตรวจลายเซ็น — ใช้เฉพาะตอนทดสอบ", flush=True)
    if args.public_url:
        print(f"      ลงทะเบียนกับ Kick ด้วย:  python kick_events.py subscribe --webhook {args.public_url}{args.path} --broadcaster <id>", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        log(f"หยุด — รับไปทั้งหมด {cfg['count']} event")
    finally:
        server.server_close()
    return 0


def cmd_verify(args) -> int:
    # โหมด 1: ตรวจไฟล์ตัวอย่าง ที่เก็บ header ไว้ในรูปแบบ 'Header: value' + บรรทัดว่าง + body
    if args.sample:
        path = Path(args.sample).expanduser()
        if not path.is_file():
            fail(f"ไม่พบไฟล์: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        head, _, body_text = text.partition("\n\n")
        headers: dict[str, str] = {}
        for line in head.splitlines():
            name, _, value = line.partition(":")
            if name.strip():
                headers[name.strip().lower()] = value.strip()
        body = body_text.encode("utf-8")
    else:
        headers = {
            "kick-event-message-id": args.message_id or "",
            "kick-event-message-timestamp": args.timestamp or "",
            "kick-event-signature": args.signature or "",
        }
        body = (args.body or "").encode("utf-8")

    message_id = headers.get("kick-event-message-id", "")
    timestamp = headers.get("kick-event-message-timestamp", "")
    signature_b64 = headers.get("kick-event-signature", "")
    if not (message_id and timestamp and signature_b64):
        fail("ต้องมี Kick-Event-Message-Id / Timestamp / Signature ครบ")

    pem_path = Path(args.public_key_file).expanduser() if args.public_key_file else None
    if pem_path and pem_path.is_file():
        pem = pem_path.read_text(encoding="utf-8")
    else:
        pem, _ = fetch_public_key(args.public_key_url)
    n, e = parse_rsa_public_key_pem(pem)
    signature = base64.b64decode(signature_b64, validate=True)
    signed = build_signed_message(message_id, timestamp, body)
    valid = rsa_verify_sha256(n, e, signature, signed)
    if valid:
        print("[OK]  ลายเซ็นถูกต้อง — ข้อความนี้มาจาก Kick จริง")
        return 0
    print("[X]   ลายเซ็นไม่ถูกต้อง", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tokens", help="ไฟล์ token (ค่าเริ่มต้น kick_tokens.json)")
    common.add_argument("--credentials", help="ไฟล์ credentials (ค่าเริ่มต้น kick_credentials.json)")
    common.add_argument("--public-key-url", dest="public_key_url", default=PUBLIC_KEY_URL,
                        help="URL ของ public key (ค่าเริ่มต้นของ Kick)")

    parser = argparse.ArgumentParser(
        prog="kick_events.py",
        description="Kick Events API (webhook) — ทางเลือกทางการแทน Pusher WebSocket",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", metavar="{subscribe,list,delete,serve,verify}")

    p = sub.add_parser("subscribe", parents=[common], help="ลงทะเบียน webhook")
    p.add_argument("--webhook", required=True, help="URL https สาธารณะที่ Kick จะยิงเข้ามา")
    p.add_argument("--broadcaster", required=True, help="broadcaster_user_id ของช่องคุณ")
    p.add_argument("--events", help=f"รายการ event คั่นด้วยจุลภาค (ค่าเริ่มต้น {DEFAULT_EVENTS})")
    p.set_defaults(func=cmd_subscribe)

    p = sub.add_parser("list", parents=[common], help="ดู subscription ที่มี")
    p.add_argument("--broadcaster", help="กรองเฉพาะ broadcaster_user_id นี้")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("delete", parents=[common], help="ยกเลิก subscription")
    p.add_argument("--id", action="append", required=True, help="subscription id (ใส่ซ้ำได้)")
    p.set_defaults(func=cmd_delete)

    p = sub.add_parser("serve", parents=[common], help="เปิดเซิร์ฟเวอร์รับ webhook ในเครื่อง")
    p.add_argument("--port", type=int, default=8081)
    p.add_argument("--bind", default="0.0.0.0", help="ที่อยู่ที่ผูกพอร์ต (ค่าเริ่มต้น 0.0.0.0)")
    p.add_argument("--path", default="/kick", help="path ที่รับ webhook (ค่าเริ่มต้น /kick)")
    p.add_argument("--no-verify", dest="verify", action="store_false", default=True,
                   help="ปิดการตรวจลายเซ็น (ใช้เฉพาะทดสอบ)")
    p.add_argument("--accept-bad", action="store_true", help="ตอบ 200 แม้ลายเซ็นผิด (ดีบัก)")
    p.add_argument("--all-events", action="store_true", help="แสดงทุก event ไม่ใช่แค่แชท")
    p.add_argument("--json-lines", action="store_true", help="พิมพ์แชทเป็น JSON 1 บรรทัด/ข้อความ")
    p.add_argument("--write", help="ต่อท้ายทุก event ลงไฟล์นี้ (NDJSON)")
    p.add_argument("--forward", help="ส่งต่อ raw body ไปยัง URL ในเครื่อง/อื่น ๆ")
    p.add_argument("--limit", type=int, default=0, help="หยุดหลังรับครบ N event")
    p.add_argument("--save-key", help="บันทึก public key ที่ดึงมาไว้ที่ไฟล์นี้")
    p.add_argument("--public-url", help="URL สาธารณะ (ใช้แสดงคำสั่ง subscribe ให้)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("verify", parents=[common], help="ตรวจลายเซ็นของ event ตัวอย่าง")
    p.add_argument("--sample", help="ไฟล์ตัวอย่าง (header lines + บรรทัดว่าง + body)")
    p.add_argument("--message-id", help="ค่า header Kick-Event-Message-Id")
    p.add_argument("--timestamp", help="ค่า header Kick-Event-Message-Timestamp")
    p.add_argument("--signature", help="ค่า header Kick-Event-Signature (base64)")
    p.add_argument("--body", help="raw body ที่เซ็น")
    p.add_argument("--public-key-file", dest="public_key_file", help="ใช้ PEM ในเครื่องแทนการดึงจากเน็ต")
    p.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"subscribe", "list", "delete", "serve", "verify"}
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["list"] + argv
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return int(args.func(args) or 0)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
