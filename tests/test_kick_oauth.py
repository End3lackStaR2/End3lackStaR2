#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ทดสอบออฟไลน์ของ kick_oauth.py (ไม่ต้องต่อเน็ต)

รัน:  python tests/test_kick_oauth.py
หรือ: python -m pytest tests/test_kick_oauth.py
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kick_oauth as k  # noqa: E402


def make_args(**kwargs) -> argparse.Namespace:
    base = {
        "credentials": None,
        "tokens": None,
        "redirect_uri": None,
        "client_id": None,
        "client_secret": None,
        "scopes": None,
    }
    base.update(kwargs)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
def test_norm_key_and_mask() -> None:
    assert k.norm_key("KICK_CLIENT_ID") == "kickclientid"
    assert k.norm_key("client-secret") == "clientsecret"
    assert k.mask("") == "(ว่าง)"
    assert k.mask("abcdefghijklmnop").startswith("abcdef")
    assert "ijklmnop" not in k.mask("abcdefghijklmnop")


def test_pkce() -> None:
    verifier, challenge = k.make_pkce()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert challenge == expected
    assert "=" not in challenge


def test_authorize_url() -> None:
    url = k.build_authorize_url("cid", "http://localhost:8080/callback", "user:read chat:write", "challenge", "st")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
    assert url.startswith(k.AUTHORIZE_URL + "?")
    assert query["client_id"] == "cid"
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["scope"] == "user:read chat:write"
    assert "redirect" not in query  # ไม่ต้องใช้ workaround กับ localhost

    url_loopback = k.build_authorize_url("cid", "http://127.0.0.1:8080/callback", "user:read", "ch", "st")
    raw_query = urllib.parse.urlparse(url_loopback).query
    assert raw_query.index("redirect=127.0.0.1") < raw_query.index("redirect_uri=")  # sacrificial param ต้องมาก่อน


def test_read_config_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        j = base / "a.json"
        j.write_text(json.dumps({"kickClientId": "abc", "nested": {"client_secret": "shh"}}), encoding="utf-8")
        cfg = k.read_config_file(j)
        assert cfg["kickclientid"] == "abc"
        assert cfg["clientsecret"] == "shh"

        env = base / "b.env"
        env.write_text('# comment\nKICK_CLIENT_ID=01XYZ\nKICK_CLIENT_SECRET="secret-with-quotes"\n', encoding="utf-8")
        cfg2 = k.read_config_file(env)
        assert cfg2["kickclientid"] == "01XYZ"
        assert cfg2["kickclientsecret"] == "secret-with-quotes"

        broken = base / "c.json"
        broken.write_text("{not json", encoding="utf-8")
        assert k.read_config_file(broken) == {}


def test_settings_precedence() -> None:
    original_dir = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        (base / "kick_credentials.json").write_text(
            json.dumps({"client_id": "FROM_JSON", "client_secret": "jsec", "redirect_uri": "http://localhost:1111/cb"}),
            encoding="utf-8",
        )
        explicit = base / "explicit.env"
        explicit.write_text(
            "KICK_CLIENT_ID=FROM_EXPLICIT\nKICK_CLIENT_SECRET=esec\nKICK_REDIRECT_URI=http://localhost:2222/cb\n",
            encoding="utf-8",
        )
        try:
            import os

            os.chdir(base)

            # ไฟล์อัตโนมัติในโฟลเดอร์ปัจจุบัน
            s = k.load_settings(make_args(tokens=str(base / "t.json")))
            assert s["client_id"] == "FROM_JSON"
            assert s["redirect_uri"] == "http://localhost:1111/cb"

            # ไฟล์ที่ระบุเองต้องทับไฟล์อัตโนมัติ
            s = k.load_settings(make_args(credentials=str(explicit), tokens=str(base / "t.json")))
            assert s["client_id"] == "FROM_EXPLICIT"
            assert s["client_secret"] == "esec"
            assert s["redirect_uri"] == "http://localhost:2222/cb"

            # argument ต้องทับทุกอย่าง
            s = k.load_settings(
                make_args(credentials=str(explicit), client_id="FROM_ARG", redirect_uri="http://localhost:3333/cb",
                          tokens=str(base / "t.json"))
            )
            assert s["client_id"] == "FROM_ARG"
            assert s["client_secret"] == "esec"
            assert s["redirect_uri"] == "http://localhost:3333/cb"

            # ค่าเริ่มต้นเมื่อไม่พบอะไรเลย
            empty = base / "empty"
            empty.mkdir()
            os.chdir(empty)
            s = k.load_settings(make_args(tokens=str(base / "t.json")))
            assert s["client_id"] is None
            assert s["redirect_uri"] == k.DEFAULT_REDIRECT_URI
            assert s["scopes"] == k.DEFAULT_SCOPES
        finally:
            os.chdir(original_dir)


def test_tokens_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sub" / "kick_tokens.json"
        tokens = k.tokens_from_response(
            {"access_token": "at", "refresh_token": "rt", "token_type": "Bearer", "expires_in": "120", "scope": "user:read"}
        )
        assert tokens["expires_in"] == 120
        assert tokens["expires_at"] > int(time.time())
        k.save_tokens(path, tokens)
        loaded = k.load_tokens(path)
        assert loaded["access_token"] == "at"
        assert loaded["refresh_token"] == "rt"
        assert "saved_at" in loaded
        assert "เหลืออีก" in k.token_expiry_text({"expires_at": int(time.time()) + 300})
        assert "หมดอายุแล้ว" in k.token_expiry_text({"expires_at": int(time.time()) - 5})


def test_callback_server() -> None:
    payload: dict = {"received": False}
    server = k.http.server.ThreadingHTTPServer(("127.0.0.1", 0), k._CallbackHandler)
    server.payload = payload  # type: ignore[attr-defined]
    server.done = threading.Event()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?code=CODE1&state=STATE1", timeout=5):
            pass
        assert server.done.wait(5)
        assert payload["code"] == "CODE1"
        assert payload["state"] == "STATE1"
        assert payload["error"] == ""
    finally:
        server.shutdown()
        server.server_close()


def main() -> int:
    tests = [v for name, v in sorted(globals().items()) if name.startswith("test_") and callable(v)]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"[FAIL] {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[ERROR] {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"[PASS] {test.__name__}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
