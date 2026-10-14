#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kick_oauth.py - Kick.com OAuth 2.1 (Authorization Code + PKCE) helper.

ใช้ได้กับ Windows + Python 3.12 ทันที ไม่ต้องติดตั้งแพ็กเกจเพิ่ม (stdlib ล้วน)

คำสั่ง:
  python kick_oauth.py setup      กรอก Client ID / Client Secret แล้วบันทึกเป็นไฟล์
  python kick_oauth.py login      เปิดเบราว์เซอร์ให้กด Authorize แล้วรับ access/refresh token
  python kick_oauth.py check      ตรวจว่า credentials / token ที่บันทึกไว้ยังใช้ได้ไหม
  python kick_oauth.py refresh    ขอ access token ใหม่จาก refresh token
  python kick_oauth.py revoke     ยกเลิก token (revoke)

ค่าเริ่มต้นจะอ่าน/เขียนไฟล์ในโฟลเดอร์เดียวกับสคริปต์นี้:
  kick_credentials.json   เก็บ client_id / client_secret
  kick_tokens.json        เก็บ access_token / refresh_token

สามารถระบุ path เองได้ด้วย --credentials และ --tokens
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import http.server
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

# ---------------------------------------------------------------------------
# ค่าคงที่ของ Kick (ตามเอกสาร https://github.com/KickEngineering/KickDevDocs)
# ---------------------------------------------------------------------------
OAUTH_BASE = "https://id.kick.com"
AUTHORIZE_URL = OAUTH_BASE + "/oauth/authorize"
TOKEN_URL = OAUTH_BASE + "/oauth/token"
REVOKE_URL = OAUTH_BASE + "/oauth/revoke"
INTROSPECT_URL = OAUTH_BASE + "/oauth/token/introspect"
API_USERS_URL = "https://api.kick.com/public/v1/users"

DEFAULT_REDIRECT_URI = "http://localhost:8080/callback"
DEFAULT_SCOPES = "user:read channel:read chat:write"

SCRIPT_DIR = Path(__file__).resolve().parent
CREDENTIAL_CANDIDATES = [
    "kick_credentials.json",
    ".kick_credentials.json",
    "credentials.json",
    "kick_credentials.env",
    "config/kick_credentials.json",
    "config/kick.json",
]

# ชื่อคีย์ที่ยอมรับในไฟล์ credentials (เทียบแบบ normalize: ตัด _ - เว้นวรรค, ตัวพิมพ์เล็ก)
ID_KEYS = ("clientid", "kickclientid", "appclientid", "oauthclientid", "kickclient")
SECRET_KEYS = ("clientsecret", "kickclientsecret", "appclientsecret", "oauthclientsecret", "secret")
REDIRECT_KEYS = ("redirecturi", "redirecturl", "callbackurl", "kickredirecturi", "callbacks", "callback")


# ---------------------------------------------------------------------------
# ยูทิลิตี้
# ---------------------------------------------------------------------------
def _fix_stdout() -> None:
    """ให้ console บน Windows แสดงผลภาษาไทยได้ และเห็นข้อความทันทีแม้ถูก redirect ลงไฟล์"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)  # type: ignore[attr-defined]
        except Exception:
            pass


def norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def mask(value: str | None, keep: int = 6) -> str:
    if not value:
        return "(ว่าง)"
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "..." + "*" * min(8, max(4, len(value) - keep))


def ok(msg: str) -> None:
    print("[OK]  " + msg)


def info(msg: str) -> None:
    print("[..] " + msg)


def warn(msg: str) -> None:
    print("[!]   " + msg)


def fail(msg: str, code: int = 1) -> "None":
    print("[X]   " + msg, file=sys.stderr)
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# อ่าน/เขียนไฟล์ config
# ---------------------------------------------------------------------------
def flatten_json(obj, out: dict, depth: int = 0) -> None:
    """รวม key จาก JSON ซ้อนชั้น (สูงสุด 3 ชั้น) ให้เป็น dict แบน แล้ว normalize ชื่อคีย์"""
    if depth > 3:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                flatten_json(v, out, depth + 1)
            elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
                out.setdefault(norm_key(str(k)), str(v))


def read_config_file(path: Path) -> dict:
    """อ่านไฟล์ credentials ได้ทั้ง JSON และรูปแบบ key=value (.env)"""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        warn(f"อ่านไฟล์ไม่ได้: {path} ({exc})")
        return {}

    result: dict = {}
    stripped = text.lstrip()
    if stripped.startswith("{") or path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            warn(f"ไฟล์ JSON ผิดรูปแบบ: {path} ({exc})")
            return {}
        flatten_json(data, result)
        return result

    # key=value / key: value / "key"="value"
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        m = re.match(r'^([A-Za-z0-9_.\-]+)\s*[:=]\s*(.*)$', line)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result.setdefault(norm_key(key), value)
    return result


def load_settings(args) -> dict:
    """รวมค่าตั้งต้นจาก: ไฟล์อัตโนมัติ -> env -> ไฟล์ที่ระบุ -> argument (ตัวหลังทับตัวหน้าเสมอ)"""
    # แต่ละ candidate = (ลำดับความสำคัญของแหล่งข้อมูล, ลำดับความสำคัญของชื่อคีย์, ค่า, แหล่งที่มา)
    sources: dict = {"client_id": [], "client_secret": [], "redirect_uri": []}

    def add(cfg: dict, src: str, priority: int) -> None:
        for field, keys in (
            ("client_id", ID_KEYS),
            ("client_secret", SECRET_KEYS),
            ("redirect_uri", REDIRECT_KEYS),
        ):
            for rank, key in enumerate(keys):
                value = cfg.get(key)
                if value is None or str(value).strip() == "":
                    continue
                sources[field].append((priority, -rank, str(value).strip(), src))

    # 1) ไฟล์ในเครื่อง (ค้นหาอัตโนมัติ)
    auto_files: list[Path] = []
    seen: set[str] = set()
    for rel in CREDENTIAL_CANDIDATES:
        for base in (SCRIPT_DIR, Path.cwd()):
            p = base / rel
            if not p.is_file():
                continue
            key = str(p.resolve())
            if key in seen:
                continue
            seen.add(key)
            auto_files.append(p)
            add(read_config_file(p), str(p), 10)

    # 2) environment variables
    add(
        {
            "clientid": os.environ.get("KICK_CLIENT_ID"),
            "clientsecret": os.environ.get("KICK_CLIENT_SECRET"),
            "redirecturi": os.environ.get("KICK_REDIRECT_URI"),
        },
        "environment variables",
        20,
    )

    # 3) ไฟล์ที่ผู้ใช้ระบุเอง
    if args.credentials:
        p = Path(args.credentials).expanduser()
        if not p.is_file():
            fail(f"ไม่พบไฟล์ credentials: {p}")
        add(read_config_file(p), str(p), 30)

    # 4) argument
    add(
        {
            "clientid": args.client_id,
            "clientsecret": args.client_secret,
            "redirecturi": args.redirect_uri,
        },
        "command line",
        40,
    )

    chosen: dict = {}
    for field, candidates in sources.items():
        if candidates:
            priority, _rank, value, src = max(candidates)
            chosen[field] = (value, src)

    settings = {
        "client_id": chosen["client_id"][0] if "client_id" in chosen else None,
        "client_secret": chosen["client_secret"][0] if "client_secret" in chosen else None,
        "redirect_uri": chosen["redirect_uri"][0] if "redirect_uri" in chosen else DEFAULT_REDIRECT_URI,
        "scopes": " ".join((args.scopes or DEFAULT_SCOPES).replace(",", " ").split()),
        "origin": {field: chosen[field][1] for field in chosen},
        "creds_file": Path(args.credentials).expanduser() if args.credentials else (auto_files[0] if auto_files else None),
    }

    tokens_default = Path(args.tokens).expanduser() if args.tokens else None
    if tokens_default is None:
        base_dir = settings["creds_file"].parent if settings["creds_file"] else SCRIPT_DIR
        tokens_default = base_dir / "kick_tokens.json"
    settings["tokens_file"] = tokens_default
    return settings


def require_credentials(settings: dict) -> None:
    missing = [name for name in ("client_id", "client_secret") if not settings.get(name)]
    if missing:
        fail(
            "ยังไม่พบ "
            + " และ ".join(missing)
            + " ให้รัน:  python kick_oauth.py setup\n"
            "      หรือระบุไฟล์เอง:  python kick_oauth.py ... --credentials kick_credentials.json"
        )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def http_post_form(url: str, data: dict, headers: dict | None = None, timeout: int = 30) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    hdrs = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    return _do_request(req, timeout)


def http_post_json(url: str, headers: dict | None = None, timeout: int = 30) -> dict:
    hdrs = {"Accept": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=b"", headers=hdrs, method="POST")
    return _do_request(req, timeout)


def _do_request(req: urllib.request.Request, timeout: int) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace") if exc.fp else ""
        detail = text.strip() or str(exc)
        fail(f"เซิร์ฟเวอร์ตอบ HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        fail(
            f"เชื่อมต่อ {urllib.parse.urlparse(req.full_url).netloc} ไม่ได้ ({exc.reason})\n"
            "      ตรวจเน็ต / ไฟร์วอลล์ / VPN แล้วลองใหม่"
        )

    if not text.strip():
        return {"_status": status}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {"_status": status, "_raw": text}
    if isinstance(data, dict):
        data.setdefault("_status", status)
        return data
    return {"_status": status, "data": data}


# ---------------------------------------------------------------------------
# token file
# ---------------------------------------------------------------------------
def load_tokens(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"อ่านไฟล์ token ไม่ได้: {path} ({exc})")
        return {}


def save_tokens(path: Path, tokens: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(tokens)
    payload["saved_at"] = int(time.time())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)  # POSIX เท่านั้น; Windows จะไม่สนใจ
    except OSError:
        pass


def tokens_from_response(resp: dict) -> dict:
    expires_in = resp.get("expires_in")
    try:
        expires_in = int(expires_in)
    except (TypeError, ValueError):
        expires_in = None

    tokens = {
        "access_token": resp.get("access_token", ""),
        "refresh_token": resp.get("refresh_token", ""),
        "token_type": resp.get("token_type", "Bearer"),
        "scope": resp.get("scope", ""),
    }
    if expires_in is not None:
        tokens["expires_in"] = expires_in
        tokens["expires_at"] = int(time.time()) + expires_in
    return tokens


def token_expiry_text(tokens: dict) -> str:
    exp = tokens.get("expires_at")
    if not exp:
        return "(ไม่ทราบเวลาหมดอายุ)"
    remaining = int(exp) - int(time.time())
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(exp)))
    if remaining <= 0:
        return f"หมดอายุแล้ว ({stamp}, {abs(remaining) // 60} นาทีที่แล้ว)"
    return f"เหลืออีก {remaining // 60} นาที (หมดอายุ {stamp})"


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------
def make_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorize_url(client_id: str, redirect_uri: str, scopes: str, challenge: str, state: str) -> str:
    params = [
        ("response_type", "code"),
        ("client_id", client_id),
    ]
    # workaround ของ Kick: ถ้า redirect URI ใช้ 127.0.0.1 ต้องมี sacrificial param มาก่อน
    host = urllib.parse.urlparse(redirect_uri).hostname or ""
    if host == "127.0.0.1":
        params.append(("redirect", "127.0.0.1"))
    params += [
        ("redirect_uri", redirect_uri),
        ("scope", scopes),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
        ("state", state),
    ]
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


# ---------------------------------------------------------------------------
# local callback server
# ---------------------------------------------------------------------------
class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server_version = "KickOAuth/1.0"

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        payload = self.server.payload  # type: ignore[attr-defined]

        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        payload["path"] = parsed.path
        payload["code"] = (query.get("code") or [""])[0]
        payload["state"] = (query.get("state") or [""])[0]
        payload["error"] = (query.get("error") or [""])[0]
        payload["error_description"] = (query.get("error_description") or [""])[0]
        payload["received"] = True

        if payload["error"]:
            title = "การอนุญาตไม่สำเร็จ"
            body = f"Kick แจ้งว่า: {payload['error']}<br>{payload['error_description']}"
        else:
            title = "ได้รับ authorization code แล้ว"
            body = "ปิดแท็บนี้ได้เลย กลับไปที่หน้าต่าง Terminal เพื่อดูผลลัพธ์"

        html = (
            "<!doctype html><html lang='th'><meta charset='utf-8'>"
            f"<title>{title}</title>"
            "<body style='font-family:Segoe UI,Tahoma,sans-serif;padding:40px;text-align:center'>"
            f"<h2>{title}</h2><p>{body}</p></body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)
        self.server.done.set()  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # ปิด log ของ http.server
        pass


def wait_for_callback(redirect_uri: str, authorize_url: str, timeout: int, open_browser: bool) -> dict:
    parsed = urllib.parse.urlparse(redirect_uri)
    if parsed.scheme != "http":
        fail(
            f"สคริปต์นี้รอ callback ได้เฉพาะ http://localhost เท่านั้น (ตอนนี้คือ {redirect_uri})\n"
            "      ถ้า redirect URI ของคุณเป็น https ให้รันแบบ manual:  python kick_oauth.py login --manual"
        )
    host = parsed.hostname or "localhost"
    port = parsed.port or 80
    if host not in ("localhost", "127.0.0.1", "::1"):
        fail(
            f"redirect URI ต้องชี้มาที่เครื่องนี้ (localhost) ไม่ใช่ {host}\n"
            "      หรือรันแบบ manual:  python kick_oauth.py login --manual"
        )

    payload: dict = {"received": False}
    bind_host = "127.0.0.1"
    try:
        server = http.server.ThreadingHTTPServer((bind_host, port), _CallbackHandler)
    except OSError as exc:
        fail(
            f"เปิดพอร์ต {port} ไม่ได้ ({exc})\n"
            "      ปิดโปรแกรมที่ใช้พอร์ตนี้อยู่ หรือใช้พอร์ตอื่น แล้วอย่าลืมแก้ redirect URI\n"
            f"      ในหน้า Developer settings ของ Kick ให้ตรงกันด้วย (เช่น http://localhost:{port + 1}/callback)"
        )
    server.payload = payload  # type: ignore[attr-defined]
    server.done = threading.Event()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print()
    print("1) เปิดลิงก์นี้ในเบราว์เซอร์ (ถ้าไม่เด้งเอง) แล้วล็อกอิน Kick + กด Authorize:")
    print("   " + authorize_url)
    print()
    print(f"2) รอรับ callback ที่ {redirect_uri} ... (กด Ctrl+C เพื่อยกเลิก)")
    if open_browser:
        try:
            webbrowser.open(authorize_url)
        except Exception:
            pass

    try:
        if not server.done.wait(timeout):
            fail(f"หมดเวลา {timeout} วินาที ยังไม่ได้รับ callback ลองใหม่อีกครั้ง")
    except KeyboardInterrupt:
        fail("ยกเลิกโดยผู้ใช้")
    finally:
        try:
            server.shutdown()
        except Exception:
            pass

    return payload


# ---------------------------------------------------------------------------
# คำสั่งย่อย
# ---------------------------------------------------------------------------
def cmd_setup(args) -> int:
    target = Path(args.credentials).expanduser() if args.credentials else (SCRIPT_DIR / "kick_credentials.json")
    if target.is_file() and not args.force:
        answer = input(f"ไฟล์ {target} มีอยู่แล้ว ต้องการเขียนทับหรือไม่? [y/N]: ").strip().lower()
        if answer not in ("y", "yes", "yes "):
            info("ยกเลิก ไม่มีการเปลี่ยนแปลง")
            return 0

    print("=== ตั้งค่า Kick Client ID / Client Secret ===")
    print("(คัดลอกจาก https://kick.com/settings/developer -> App ของคุณ)")
    print()
    client_id = input("Client ID: ").strip()
    if not client_id:
        fail("Client ID ว่างเปล่า ยกเลิก")
    client_secret = getpass.getpass("Client Secret (จะไม่แสดงบนหน้าจอ): ").strip()
    if not client_secret:
        fail("Client Secret ว่างเปล่า ยกเลิก")

    default_redirect = DEFAULT_REDIRECT_URI
    redirect = input(f"Redirect URI (Enter เพื่อใช้ค่าเริ่มต้น {default_redirect}): ").strip() or default_redirect

    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass

    print()
    ok(f"บันทึกแล้ว: {target}")
    print(f"      client_id     = {mask(client_id)}")
    print("      client_secret = (บันทึกแล้ว, ไม่แสดง)")
    print(f"      redirect_uri  = {redirect}")
    print()
    info("ขั้นต่อไป: ยืนยันว่า redirect URI ข้างบนถูกตั้งในหน้า Developer settings ของ Kick")
    info("แล้วรัน:  python kick_oauth.py login")
    return 0


def cmd_login(args) -> int:
    settings = load_settings(args)
    require_credentials(settings)

    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(24)
    authorize_url = build_authorize_url(
        settings["client_id"], settings["redirect_uri"], settings["scopes"], challenge, state
    )

    if args.manual:
        print("เปิดลิงก์นี้ในเบราว์เซอร์:")
        print(authorize_url)
        print()
        raw = input("วาง URL ปลายทาง (หรือ code) แล้วกด Enter: ").strip()
        if not raw:
            fail("ไม่ได้กรอกอะไร ยกเลิก")
        if "code=" in raw or raw.startswith("http"):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(raw).query)
            code = (query.get("code") or [""])[0]
            got_state = (query.get("state") or [""])[0]
            if not code:
                fail("ไม่พบ code ใน URL ที่วางมา")
            if got_state and got_state != state:
                fail("state ไม่ตรงกัน (อาจถูกลักลอบ) ยกเลิกเพื่อความปลอดภัย")
        else:
            code = raw
    else:
        payload = wait_for_callback(
            settings["redirect_uri"], authorize_url, args.timeout, not args.no_browser
        )
        if not payload.get("received"):
            fail("ไม่ได้รับ callback")
        if payload.get("error"):
            fail(f"Kick ปฏิเสธ: {payload['error']} {payload.get('error_description', '')}")
        if payload.get("state") != state:
            fail("state ไม่ตรงกัน ยกเลิกเพื่อความปลอดภัย")
        code = payload.get("code") or ""
        if not code:
            fail("ไม่พบ authorization code ใน callback")

    info("กำลังแลก code เป็น token ...")
    resp = http_post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": settings["client_id"],
            "client_secret": settings["client_secret"],
            "redirect_uri": settings["redirect_uri"],
            "code_verifier": verifier,
            "code": code,
        },
    )
    tokens = tokens_from_response(resp)
    if not tokens.get("access_token"):
        fail("ไม่ได้รับ access_token จาก Kick: " + json.dumps(resp, ensure_ascii=False)[:400])

    save_tokens(settings["tokens_file"], tokens)
    print()
    ok("ได้ token เรียบร้อย")
    print(f"      scope      : {tokens.get('scope') or '(ไม่ระบุ)'}")
    print(f"      access_token : {mask(tokens.get('access_token'))}")
    print(f"      refresh_token: {mask(tokens.get('refresh_token'))}")
    print(f"      อายุ token  : {token_expiry_text(tokens)}")
    print(f"      บันทึกไว้ที่ : {settings['tokens_file']}")
    print()
    info("ทดสอบยิง API:  python kick_oauth.py check")
    return 0


def cmd_refresh(args) -> int:
    settings = load_settings(args)
    require_credentials(settings)
    tokens = load_tokens(settings["tokens_file"])
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        fail(f"ไม่พบ refresh_token ใน {settings['tokens_file']} — ต้อง login ใหม่ก่อน")

    info("กำลังขอ access token ใหม่ ...")
    resp = http_post_form(
        TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "client_id": settings["client_id"],
            "client_secret": settings["client_secret"],
            "refresh_token": refresh_token,
        },
    )
    new_tokens = tokens_from_response(resp)
    if not new_tokens.get("access_token"):
        fail("refresh ไม่สำเร็จ: " + json.dumps(resp, ensure_ascii=False)[:400])
    if not new_tokens.get("refresh_token"):
        new_tokens["refresh_token"] = refresh_token

    save_tokens(settings["tokens_file"], new_tokens)
    ok("refresh สำเร็จ")
    print(f"      access_token : {mask(new_tokens.get('access_token'))}")
    print(f"      อายุ token  : {token_expiry_text(new_tokens)}")
    print(f"      บันทึกไว้ที่ : {settings['tokens_file']}")
    return 0


def cmd_check(args) -> int:
    settings = load_settings(args)
    print("=== ตรวจสอบการตั้งค่า ===")
    creds = settings["creds_file"]
    print(f"      ไฟล์ credentials : {creds if creds else '(ไม่พบไฟล์ ใช้ env/argument)'}")
    print(f"      client_id        : {mask(settings.get('client_id'))}"
          + (f"   [จาก {settings['origin'].get('client_id')}]" if settings.get("client_id") else ""))
    print(f"      client_secret    : {'(พบ)' if settings.get('client_secret') else '(ไม่พบ)'}"
          + (f"   [จาก {settings['origin'].get('client_secret')}]" if settings.get("client_secret") else ""))
    print(f"      redirect_uri     : {settings['redirect_uri']}")
    print(f"      scopes (ค่าเริ่มต้น): {settings['scopes']}")
    print(f"      ไฟล์ token        : {settings['tokens_file']}")

    tokens = load_tokens(settings["tokens_file"])
    if tokens.get("access_token"):
        print()
        print(f"      token             : {mask(tokens.get('access_token'))}")
        print(f"      scope ที่ได้      : {tokens.get('scope') or '(ไม่ระบุ)'}")
        print(f"      อายุ              : {token_expiry_text(tokens)}")
    else:
        warn("ยังไม่มี token ในไฟล์ — รัน:  python kick_oauth.py login")

    if not settings.get("client_id") or not settings.get("client_secret"):
        fail("ข้อมูล credentials ไม่ครบ — รัน:  python kick_oauth.py setup")

    access = tokens.get("access_token")
    if not access:
        return 0

    print()
    info("ทดสอบเรียก API (users) ...")
    try:
        req = urllib.request.Request(
            API_USERS_URL,
            headers={"Authorization": f"Bearer {access}", "Accept": "application/json"},
            method="GET",
        )
        resp = _do_request(req, 30)
        data = resp.get("data")
        if isinstance(data, list) and data:
            user = data[0]
            ok(f"เรียก API สำเร็จ: {user.get('name')} (id {user.get('user_id') or user.get('id')})")
        else:
            ok("เรียก API สำเร็จ: " + json.dumps(resp, ensure_ascii=False)[:300])
    except SystemExit:
        warn("เรียก API ไม่สำเร็จ (token อาจหมดอายุ — ลอง: python kick_oauth.py refresh)")
        return 1

    print()
    info("ตรวจ token กับ id.kick.com ...")
    try:
        resp = http_post_json(INTROSPECT_URL, {"Authorization": f"Bearer {access}"})
        payload = resp.get("data") or resp
        if payload.get("active"):
            ok(f"token ยังใช้งานได้ (type={payload.get('token_type')}, scope={payload.get('scope')})")
        else:
            warn("Kick แจ้งว่า token นี้ไม่ active แล้ว — ลอง refresh หรือ login ใหม่")
    except SystemExit:
        warn("introspect ไม่สำเร็จ (ข้ามได้)")
    return 0


def cmd_revoke(args) -> int:
    settings = load_settings(args)
    tokens = load_tokens(settings["tokens_file"])
    token = tokens.get("refresh_token") or tokens.get("access_token")
    if not token:
        fail(f"ไม่พบ token ใน {settings['tokens_file']}")
    hint = "refresh_token" if tokens.get("refresh_token") else "access_token"

    if not args.yes:
        answer = input(f"ยืนยันยกเลิก token ({hint}) ของแอปนี้? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            info("ยกเลิกคำสั่ง")
            return 0

    url = REVOKE_URL + "?" + urllib.parse.urlencode({"token": token, "token_hint_type": hint})
    http_post_form(url, {})
    ok("revoke token เรียบร้อย (ฝั่ง Kick)")
    try:
        settings["tokens_file"].unlink()
        ok(f"ลบไฟล์ token แล้ว: {settings['tokens_file']}")
    except OSError as exc:
        warn(f"ลบไฟล์ token ไม่ได้: {exc}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--credentials", metavar="FILE", help="ไฟล์เก็บ client_id/client_secret (JSON หรือ .env)")
    common.add_argument("--tokens", metavar="FILE", help="ไฟล์เก็บ token (ค่าเริ่มต้น kick_tokens.json)")
    common.add_argument("--redirect-uri", dest="redirect_uri", help=f"ค่าเริ่มต้น {DEFAULT_REDIRECT_URI}")
    common.add_argument("--client-id", dest="client_id", help="ระบุ client id ตรง ๆ (ไม่ต้องใช้ไฟล์)")
    common.add_argument("--client-secret", dest="client_secret", help="ระบุ client secret ตรง ๆ (ไม่แนะนำ)")
    common.add_argument("--scopes", help=f"พื้นที่ขออนุญาต (ค่าเริ่มต้น '{DEFAULT_SCOPES}')")

    parser = argparse.ArgumentParser(
        prog="kick_oauth.py",
        description="Kick.com OAuth 2.1 (PKCE) helper — รับ access/refresh token ได้ในคำสั่งเดียว",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="command", metavar="{setup,login,check,refresh,revoke}")

    p = sub.add_parser("setup", parents=[common], help="บันทึก Client ID / Secret")
    p.add_argument("--force", action="store_true", help="เขียนทับไฟล์เดิมโดยไม่ถาม")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("login", parents=[common], help="เริ่ม OAuth แล้วรับ token (ค่าเริ่มต้น)")
    p.add_argument("--manual", action="store_true", help="ไม่เปิด local server ให้คัดลอก code เอง")
    p.add_argument("--no-browser", action="store_true", help="ไม่ต้องเปิดเบราว์เซอร์อัตโนมัติ")
    p.add_argument("--timeout", type=int, default=300, help="วินาทีที่รอ callback (ค่าเริ่มต้น 300)")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("check", parents=[common], help="ตรวจ credentials/token และทดสอบเรียก API")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("refresh", parents=[common], help="ขอ access token ใหม่")
    p.set_defaults(func=cmd_refresh)

    p = sub.add_parser("revoke", parents=[common], help="ยกเลิก token")
    p.add_argument("--yes", action="store_true", help="ไม่ต้องถามยืนยัน")
    p.set_defaults(func=cmd_revoke)
    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_stdout()
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"setup", "login", "check", "refresh", "revoke"}
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help")):
        argv = ["login"] + argv  # ไม่ระบุคำสั่ง = login

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
        fail("ยกเลิกโดยผู้ใช้ (Ctrl+C)", 130)
    except socket.timeout:
        fail("หมดเวลาเชื่อมต่อเครือข่าย ลองใหม่อีกครั้ง")
