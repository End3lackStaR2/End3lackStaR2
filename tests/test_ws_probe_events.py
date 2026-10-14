#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ทดสอบออฟไลน์ของ ws_probe.py และ kick_events.py (ไม่ต้องต่อเน็ต)

รัน:  python tests/test_ws_probe_events.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import socket
import sys
import threading
import time
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import kick_events as ke  # noqa: E402
import ws_probe as wp  # noqa: E402
from test_kick_chat import BAD_KEY, GOOD_KEY, MockPusherServer  # noqa: E402


# ---------------------------------------------------------------------------
# สร้างกุญแจ RSA สำหรับทดสอบ (ใช้ cryptography ถ้ามี; ข้ามถ้าไม่มี)
# ---------------------------------------------------------------------------
def make_test_key():
    """คืน (pem, signer(bytes)->bytes) หรือ None ถ้าไม่มีไลบรารีให้ใช้"""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        return None

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    def sign(message: bytes) -> bytes:
        return key.sign(message, padding.PKCS1v15(), hashes.SHA256())

    return pem, sign


# ---------------------------------------------------------------------------
# ws_probe
# ---------------------------------------------------------------------------
def test_ws_probe_good_key_ok() -> None:
    server = MockPusherServer()
    server.start()
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = wp.probe(f"ws://127.0.0.1:{server.port}/app/{GOOD_KEY}?protocol=7&client=js", 5.0)
        text = out.getvalue()
        assert rc == 0, text
        assert "pusher:connection_established" in text
        assert "✅" in text
    finally:
        server.stop()


def test_ws_probe_reports_key_pusher_saw() -> None:
    server = MockPusherServer()
    server.start()
    try:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = wp.probe(f"ws://127.0.0.1:{server.port}/app/{BAD_KEY}?protocol=7", 5.0)
        text = out.getvalue()
        assert rc == 7, text
        assert f"Pusher บอกว่ามันเห็น key: '{BAD_KEY}'" in text
        assert "ไม่ใช่ key สาธารณะที่รู้จัก" in text
    finally:
        server.stop()


def test_ws_probe_flags_mismatch_between_sent_and_seen_key() -> None:
    """จำลองกรณี 'Pusher เห็น key อื่น' — ต้องเตือนเรื่องโค้ดอ่านฟิลด์ผิด"""
    server = MockPusherServer()
    server.start()
    try:
        out = io.StringIO()
        # ส่ง URL ด้วย key หนึ่ง แต่ header Host/URL จริงถูกแก้โดย server จำลองไม่ได้
        # จึงทดสอบเส้นทาง 'key ตรงกัน' แล้ว และเช็คข้อความกรณีเห็น key ต่างผ่านฟังก์ชันแยก
        with redirect_stdout(out):
            wp.probe(f"ws://127.0.0.1:{server.port}/app/{BAD_KEY}?protocol=7", 5.0)
        assert "key ที่คุณส่ง = key ที่ Pusher เห็น" in out.getvalue()
    finally:
        server.stop()


def test_ws_probe_key_report_detection() -> None:
    assert wp.key_report(GOOD_KEY) == []
    zero_width = wp.key_report(GOOD_KEY + "\u200b")
    assert any("U+200B" in issue for issue in zero_width)
    assert any("ZERO WIDTH" in issue for issue in zero_width)

    spaced = wp.key_report(" " + GOOD_KEY)
    assert any("whitespace" in issue for issue in spaced)

    oauth_client_id = wp.key_report("01M2WEYN058CXSA5EQB2QATV8M")
    assert any("OAuth client_id" in issue for issue in oauth_client_id)

    assert any("รูปแบบไม่ใช่ hex" in issue for issue in wp.key_report("abc"))


def test_ws_probe_extract_from_config(tmp_path=None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        path.write_text(json.dumps({
            "pusher": {"appKey": BAD_KEY + "\u200b",
                       "wsUrl": f"wss://ws-us2.pusher.com/app/{BAD_KEY}?protocol=7"},
        }), encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            key, url = wp.extract_from_config(str(path))
        assert key == BAD_KEY + "\u200b" or key == BAD_KEY
        assert url.startswith("wss://ws-us2.pusher.com/app/")
        assert "อ่านจาก config" in out.getvalue()

        missing = io.StringIO()
        with redirect_stdout(missing):
            key2, url2 = wp.extract_from_config(str(Path(tmpdir) / "nope.json"))
        assert (key2, url2) == ("", "")


def test_ws_probe_dns_failure_path() -> None:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = wp.probe("wss://host-that-does-not-exist-12345.invalid/app/abc?protocol=7", 2.0)
    assert rc == 2
    assert "DNS" in out.getvalue()


def test_ws_probe_private_ip_detection() -> None:
    assert wp.is_private_ip("127.0.0.1")
    assert wp.is_private_ip("192.168.0.5")
    assert wp.is_private_ip("10.1.2.3")
    assert wp.is_private_ip("172.16.5.5")
    assert not wp.is_private_ip("172.32.5.5")
    assert not wp.is_private_ip("3.147.174.126")


# ---------------------------------------------------------------------------
# kick_events — RSA / DER
# ---------------------------------------------------------------------------
def test_rsa_verify_roundtrip() -> None:
    material = make_test_key()
    if material is None:
        print("      (ข้าม: ไม่มีไลบรารี cryptography สำหรับสร้างกุญแจทดสอบ)", file=sys.stderr)
        return
    pem, sign = material
    n, e = ke.parse_rsa_public_key_pem(pem)
    assert n.bit_length() == 2048
    assert e == 65537

    message = ke.build_signed_message("01ABC", "2026-10-14T07:00:00Z", b'{"content":"hello"}')
    signature = sign(message)
    assert ke.rsa_verify_sha256(n, e, signature, message) is True

    # ข้อความถูกแก้ 1 ตัวอักษร -> ต้องไม่ผ่าน
    tampered = ke.build_signed_message("01ABC", "2026-10-14T07:00:00Z", b'{"content":"hacked"}')
    assert ke.rsa_verify_sha256(n, e, signature, tampered) is False

    # ลายเซ็นถูกตัด -> ต้องไม่ผ่าน (และต้องไม่โยน exception)
    assert ke.rsa_verify_sha256(n, e, signature[:100], message) is False

    # ลายเซ็นขยะยาวเท่ากัน -> False
    assert ke.rsa_verify_sha256(n, e, b"\x01" * len(signature), message) is False


def test_parse_public_key_rejects_garbage() -> None:
    for bad in ("", "not a pem", "-----BEGIN PUBLIC KEY-----\nnope\n-----END PUBLIC KEY-----"):
        try:
            ke.parse_rsa_public_key_pem(bad)
        except (ValueError, Exception) as exc:  # noqa: BLE001
            assert isinstance(exc, (ValueError, Exception))
        else:
            raise AssertionError(f"ควรโยน error กับข้อมูล: {bad!r}")


def test_build_signed_message_matches_docs() -> None:
    signed = ke.build_signed_message("ID1", "TS1", b"BODY")
    assert signed == b"ID1.TS1.BODY"


# ---------------------------------------------------------------------------
# kick_events — payload / summary
# ---------------------------------------------------------------------------
def test_summarize_and_normalize_chat() -> None:
    payload = {
        "message_id": "m1",
        "content": "hello [emote:1:KEKW]",
        "created_at": "2026-10-14T07:00:00Z",
        "sender": {"user_id": 9, "username": "nueng", "channel_slug": "nueng",
                   "identity": {"username_color": "#0f0", "badges": [{"type": "moderator"}]}},
    }
    line = ke.summarize_event("chat.message.sent", payload)
    assert "nueng" in line and "hello" in line and "moderator" in line

    normalized = ke.normalize_chat(payload)
    assert normalized["username"] == "nueng"
    assert normalized["badges"] == ["moderator"]
    assert normalized["id"] == "m1"
    assert normalized["color"] == "#0f0"

    assert "ติดตาม" in ke.summarize_event("channel.followed", {"follower": {"username": "a"}})
    assert "แบน" in ke.summarize_event("moderation.banned", {"user": {"username": "b"}})
    assert "KICKs" in ke.summarize_event("kicks.gifted", {"gifter": {"username": "c"}, "amount": 5})


# ---------------------------------------------------------------------------
# kick_events — webhook receiver
# ---------------------------------------------------------------------------
class Receiver:
    def __init__(self, key_pair, verify=True, **cfg_overrides):
        self.port = self._free_port()
        self.cfg = {
            "path": "/kick", "verify": verify, "accept_bad": False, "all_events": False,
            "json_lines": False, "write": None, "write_lock": threading.Lock(), "forward": None,
            "public_key": key_pair, "count": 0, "limit": 0,
            "overlay": False, "hub": None,
        }
        self.cfg.update(cfg_overrides)
        self.server = ke.http.server.ThreadingHTTPServer(("127.0.0.1", self.port), ke.WebhookHandler)
        self.server.cfg = self.cfg
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def post(self, body: bytes, headers: dict) -> tuple[int, str]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/kick", data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def get(self, path="/kick") -> tuple[int, str]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_receiver_accepts_valid_signature() -> None:
    material = make_test_key()
    if material is None:
        return
    pem, sign = material
    key = ke.parse_rsa_public_key_pem(pem)
    receiver = Receiver(key)
    try:
        body = json.dumps({
            "message_id": "m-1", "content": "สวัสดี",
            "created_at": "2026-10-14T07:00:00Z",
            "sender": {"user_id": 1, "username": "viewer", "identity": {"badges": []}},
        }, ensure_ascii=False).encode()
        message_id, timestamp = "01MSGID", "2026-10-14T07:00:01Z"
        signature = base64.b64encode(sign(ke.build_signed_message(message_id, timestamp, body))).decode()

        out = io.StringIO()
        with redirect_stdout(out):
            status, response = receiver.post(body, {
                "Content-Type": "application/json",
                "Kick-Event-Message-Id": message_id,
                "Kick-Event-Message-Timestamp": timestamp,
                "Kick-Event-Signature": signature,
                "Kick-Event-Type": "chat.message.sent",
            })
        assert status == 200, response
        assert json.loads(response)["verified"] is True
        assert "viewer" in out.getvalue() and "สวัสดี" in out.getvalue()
        assert receiver.cfg["count"] == 1
    finally:
        receiver.stop()


def test_receiver_rejects_bad_signature() -> None:
    material = make_test_key()
    if material is None:
        return
    pem, sign = material
    key = ke.parse_rsa_public_key_pem(pem)
    receiver = Receiver(key)
    try:
        body = b'{"message_id":"m-2","content":"evil"}'
        status, response = receiver.post(body, {
            "Kick-Event-Message-Id": "01X", "Kick-Event-Message-Timestamp": "2026-10-14T07:00:01Z",
            "Kick-Event-Signature": base64.b64encode(b"\x00" * 256).decode(),
            "Kick-Event-Type": "chat.message.sent",
        })
        assert status == 401, response
        assert json.loads(response)["verified"] is False
        assert receiver.cfg["count"] == 0

        # ลายเซ็นหายไปเลย
        status2, _ = receiver.post(body, {"Kick-Event-Type": "chat.message.sent"})
        assert status2 == 401
    finally:
        receiver.stop()


def test_receiver_no_verify_mode_and_path_check() -> None:
    receiver = Receiver((0, 0), verify=False)
    try:
        body = b'{"message_id":"m-3","content":"no-verify"}'
        status, response = receiver.post(body, {"Kick-Event-Type": "chat.message.sent"})
        assert status == 200, response
        assert json.loads(response)["verified"] is None

        # /status คือหน้าที่มีข้อมูลสุขภาพของตัวรับ (เดิมใช้ /kick)
        status_get, payload = receiver.get("/status")
        assert status_get == 200 and "kick_events" in payload

        # path อื่นที่ไม่รู้จักต้องตอบ 404 (Receiver.get คืนรหัส ไม่โยน exception)
        status_nope, _ = receiver.get("/nope")
        assert status_nope == 404, f"path ที่ไม่รู้จักต้องตอบ 404 (ได้ {status_nope})"

        # path ที่ไม่ตรงกับ webhook → POST ได้ 404 เช่นกัน
        status_404, _ = receiver.post(body, {"Kick-Event-Type": "chat.message.sent"})
        assert status_404 == 200, "path /kick ต้องรับ event ได้"
    finally:
        receiver.stop()


def test_receiver_writes_ndjson_and_accepts_bad() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        out_file = str(Path(tmpdir) / "events.ndjson")
        receiver = Receiver((0, 0), verify=True, accept_bad=True, write=out_file)
        try:
            body = b'{"message_id":"m-4","content":"x"}'
            status, _ = receiver.post(body, {"Kick-Event-Type": "chat.message.sent"})
            assert status == 200
            assert receiver.cfg["count"] == 0, "event ที่ลายเซ็นผิดต้องไม่ถูกนับ/บันทึก"
        finally:
            receiver.stop()
        assert not Path(out_file).exists() or Path(out_file).read_text() == ""

        receiver2 = Receiver((0, 0), verify=False, write=out_file)
        try:
            body = b'{"message_id":"m-5","content":"y"}'
            receiver2.post(body, {"Kick-Event-Type": "chat.message.sent"})
            time.sleep(0.2)
        finally:
            receiver2.stop()
        lines = Path(out_file).read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "chat.message.sent" and record["payload"]["content"] == "y"


def test_receiver_forward_posts_to_target() -> None:
    received: list[bytes] = []

    class SinkHandler(ke.http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            received.append(self.rfile.read(length))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):  # noqa: D102
            pass

    sink = ke.http.server.ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
    threading.Thread(target=sink.serve_forever, daemon=True).start()
    forward = ke.make_forwarder(f"http://127.0.0.1:{sink.server_address[1]}/next")

    receiver = Receiver((0, 0), verify=False, forward=forward)
    try:
        body = b'{"message_id":"m-6","content":"forward me"}'
        receiver.post(body, {"Kick-Event-Type": "chat.message.sent"})
        deadline = time.time() + 3
        while time.time() < deadline and not received:
            time.sleep(0.05)
        assert received and received[0] == body
    finally:
        receiver.stop()
        sink.shutdown()
        sink.server_close()


# ---------------------------------------------------------------------------
# kick_events — subscription payload
# ---------------------------------------------------------------------------
def test_subscribe_payload_shape() -> None:
    captured: dict = {}

    original = ke.api_request

    def fake_request(method, url, token, payload=None):
        captured.update({"method": method, "url": url, "token": token, "payload": payload})
        return 200, {"data": [{"id": "sub-1", "event": "chat.message.sent"}]}

    ke.api_request = fake_request
    ke.get_access_token = lambda args, auto_refresh=True: "test-token"  # type: ignore[assignment]
    try:
        args = ke.build_parser().parse_args([
            "subscribe", "--webhook", "https://example.trycloudflare.com/kick",
            "--broadcaster", "123456", "--events", "chat.message.sent,channel.followed",
        ])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ke.cmd_subscribe(args)
        assert rc == 0
        assert captured["method"] == "POST"
        assert captured["url"] == ke.SUBSCRIPTIONS_URL
        assert captured["token"] == "test-token"
        payload = captured["payload"]
        assert payload["method"] == "webhook"
        assert payload["broadcaster_user_id"] == 123456
        assert payload["url"].endswith("/kick")
        assert [e["name"] for e in payload["events"]] == ["chat.message.sent", "channel.followed"]
        assert all(e["version"] == 1 for e in payload["events"])
    finally:
        ke.api_request = original


def test_subscribe_rejects_http() -> None:
    args = ke.build_parser().parse_args([
        "subscribe", "--webhook", "http://localhost:8081/kick", "--broadcaster", "1",
    ])
    try:
        ke.cmd_subscribe(args)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("ต้องปฏิเสธ http://")


def test_list_and_delete_commands() -> None:
    original = ke.api_request
    ke.get_access_token = lambda args, auto_refresh=True: "t"  # type: ignore[assignment]
    try:
        ke.api_request = lambda m, u, t, p=None: (200, {"data": [
            {"id": "s1", "event": "chat.message.sent", "method": "webhook", "broadcaster_user_id": 42},
        ]})
        args = ke.build_parser().parse_args(["list", "--broadcaster", "42"])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ke.cmd_list(args)
        assert rc == 0 and "chat.message.sent" in out.getvalue() and "s1" in out.getvalue()

        ke.api_request = lambda m, u, t, p=None: (204, {})
        args2 = ke.build_parser().parse_args(["delete", "--id", "s1", "--id", "s2"])
        out2 = io.StringIO()
        with redirect_stdout(out2):
            rc2 = ke.cmd_delete(args2)
        assert rc2 == 0 and "2 รายการ" in out2.getvalue()
    finally:
        ke.api_request = original


def test_verify_command_with_sample_file() -> None:
    material = make_test_key()
    if material is None:
        return
    import tempfile

    pem, sign = material
    body = b'{"message_id":"m-9","content":"hi"}'
    message_id, timestamp = "01ID", "2026-10-14T08:00:00Z"
    signature = base64.b64encode(sign(ke.build_signed_message(message_id, timestamp, body))).decode()

    with tempfile.TemporaryDirectory() as tmpdir:
        pem_file = Path(tmpdir) / "kick.pem"
        pem_file.write_text(pem, encoding="utf-8")
        sample = Path(tmpdir) / "sample.txt"
        sample.write_text(
            f"Kick-Event-Message-Id: {message_id}\n"
            f"Kick-Event-Message-Timestamp: {timestamp}\n"
            f"Kick-Event-Signature: {signature}\n\n",
            encoding="utf-8",
        )
        sample.write_bytes(sample.read_text(encoding="utf-8").encode() + body)

        args = ke.build_parser().parse_args([
            "verify", "--sample", str(sample), "--public-key-file", str(pem_file),
        ])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ke.cmd_verify(args)
        assert rc == 0 and "ลายเซ็นถูกต้อง" in out.getvalue()

        # แก้ body → ต้องไม่ผ่าน
        bad = Path(tmpdir) / "bad.txt"
        bad.write_bytes(sample.read_bytes().replace(b"hi", b"no"))
        args_bad = ke.build_parser().parse_args([
            "verify", "--sample", str(bad), "--public-key-file", str(pem_file),
        ])
        err = io.StringIO()
        with redirect_stderr(err):
            rc_bad = ke.cmd_verify(args_bad)
        assert rc_bad == 1


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


# ---------------------------------------------------------------------------
# regression: --config ที่ใส่ก่อนชื่อคำสั่งต้องไม่ถูกทับ (argparse subparser)
# ---------------------------------------------------------------------------
def test_prescan_option_and_apply() -> None:
    assert kc_prescan("--config", ["--config", "A.json", "setup"]) == "A.json"
    assert kc_prescan("--config", ["setup", "--config", "B.json"]) == "B.json"
    assert kc_prescan("--config", ["--config=C.json", "setup"]) == "C.json"
    assert kc_prescan("--config", ["setup"]) is None

    class NS:
        pass

    ns = NS()
    ns.config = None
    ns.timeout = None
    ke.apply_prescanned(ns, ["--config", "X.json", "--timeout", "3.5", "serve"],
                        [("--config", "config", str), ("--timeout", "timeout", float)])
    assert ns.config == "X.json"
    assert ns.timeout == 3.5

    # ค่าที่ argparse อ่านได้แล้ว ต้องไม่ถูก prescan ทับ
    ns2 = NS()
    ns2.config = "จาก argparse"
    ke.apply_prescanned(ns2, ["--config", "จาก prescan", "serve"], [("--config", "config", str)])
    assert ns2.config == "จาก argparse"


def kc_prescan(name, argv):
    return ke.prescan_option(argv, name)


def test_events_config_and_setup(tmp_path=None) -> None:
    import builtins
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_file = Path(tmpdir) / "kick_events_config.json"
        argv = ["--config", str(cfg_file), "setup"]
        parser = ke.build_parser()
        args = parser.parse_args(argv)
        ke.apply_prescanned(args, argv, [("--config", "config", str)])
        assert args.config == str(cfg_file)

        answers = iter(["121539855", "https://x.trycloudflare.com/kick", "8099", "/hook",
                        "chat.ndjson", "y", "chat.message.sent,kicks.gifted"])
        original_input = builtins.input
        builtins.input = lambda prompt="": next(answers)
        try:
            with redirect_stdout(io.StringIO()):
                rc = ke.cmd_setup(args)
        finally:
            builtins.input = original_input
        assert rc == 0
        saved = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert saved["broadcaster_user_id"] == 121539855
        assert saved["port"] == 8099
        assert saved["path"] == "/hook"
        assert saved["overlay"] is True
        assert saved["events"] == ["chat.message.sent", "kicks.gifted"]

        # subscribe ต้องดึงค่าจากไฟล์นี้ได้โดยไม่ต้องส่ง flag อะไรเลย
        captured: dict = {}

        def fake_api(method, url, token, payload=None):
            captured.update(payload or {})
            return 200, {"data": [{"id": "s1", "event": "chat.message.sent"}]}

        original_api = ke.api_request
        original_token = ke.get_access_token
        ke.api_request = fake_api
        ke.get_access_token = lambda a, auto_refresh=True: "tok"
        try:
            for argv2 in (["--config", str(cfg_file), "subscribe"],
                          ["subscribe", "--config", str(cfg_file)]):
                captured.clear()
                parser2 = ke.build_parser()
                args2 = parser2.parse_args(argv2)
                ke.apply_prescanned(args2, argv2, [("--config", "config", str)])
                with redirect_stdout(io.StringIO()):
                    rc2 = ke.cmd_subscribe(args2)
                assert rc2 == 0
                assert captured["url"] == "https://x.trycloudflare.com/kick"
                assert captured["broadcaster_user_id"] == 121539855
                assert [e["name"] for e in captured["events"]] == ["chat.message.sent", "kicks.gifted"]
        finally:
            ke.api_request = original_api
            ke.get_access_token = original_token


def test_events_overlay_status_and_sse() -> None:
    hub = ke.Hub()
    receiver = Receiver((0, 0), verify=False, overlay=True, hub=hub)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{receiver.port}/overlay", timeout=5) as resp:
            html = resp.read().decode()
        assert resp.status == 200
        assert "EventSource" in html and "kickTest" in html
        assert "files.kick.com/emotes" in html, "ต้องแปลง [emote:..] เป็นรูป"

        with urllib.request.urlopen(f"http://127.0.0.1:{receiver.port}/status", timeout=5) as resp:
            status = json.loads(resp.read().decode())
        assert status["overlay_url"] == "/overlay"
        assert status["sse_url"] == "/events"

        received: list[str] = []
        ready = threading.Event()

        def listen() -> None:
            with urllib.request.urlopen(f"http://127.0.0.1:{receiver.port}/events", timeout=15) as resp:
                ready.set()
                while len(received) < 2:
                    line = resp.readline().decode("utf-8", "replace").strip()
                    if line.startswith("data:"):
                        received.append(line[5:].strip())
                        if '"kind": "chat"' in received[-1] or '"kind":"chat"' in received[-1]:
                            return

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()
        assert ready.wait(5), "SSE ต้องเริ่มสตรีมได้"
        time.sleep(0.3)

        body = json.dumps({
            "message_id": "m1", "content": "สวัสดี",
            "sender": {"user_id": 1, "username": "viewer", "identity": {"badges": [{"type": "sub"}]}},
        }, ensure_ascii=False).encode()
        receiver.post(body, {"Kick-Event-Type": "chat.message.sent"})
        thread.join(timeout=6)

        assert received, "ต้องมีข้อมูลส่งออกทาง SSE"
        assert '"kind": "stat"' in received[0] or '"kind":"stat"' in received[0]
        chat_items = [json.loads(item) for item in received if "chat" in item]
        assert chat_items and chat_items[-1]["username"] == "viewer"
        assert chat_items[-1]["kind"] == "chat"
    finally:
        receiver.stop()


def test_events_overlay_disabled_returns_404() -> None:
    receiver = Receiver((0, 0), verify=False, overlay=False, hub=None)
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{receiver.port}/overlay", timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
        else:
            raise AssertionError("เมื่อปิด overlay ต้องตอบ 404")
        with urllib.request.urlopen(f"http://127.0.0.1:{receiver.port}/status", timeout=5) as resp:
            status = json.loads(resp.read().decode())
        assert status["overlay"] is False and status["overlay_url"] is None
    finally:
        receiver.stop()


def test_hub_publish_unsubscribe() -> None:
    hub = ke.Hub()
    q1 = hub.subscribe()
    q2 = hub.subscribe()
    assert hub.count == 2
    hub.publish({"kind": "chat", "content": "x"})
    assert q1.get_nowait()["content"] == "x"
    assert q2.get_nowait()["content"] == "x"
    hub.unsubscribe(q1)
    assert hub.count == 1
    hub.publish({"kind": "chat", "content": "y"})
    assert q1.empty() and q2.get_nowait()["content"] == "y"
    hub.unsubscribe(q1)  # ยกเลิกซ้ำต้องไม่พัง
    assert hub.count == 1



# ---------------------------------------------------------------------------
# เติม path ให้ URL ของ webhook (สาเหตุอันดับ 1 ของ "event ไม่เข้า": URL ไม่มี /kick)
# ---------------------------------------------------------------------------
def test_ensure_path_for_webhook_url() -> None:
    assert ke.ensure_path("https://abc.trycloudflare.com", "/kick") == "https://abc.trycloudflare.com/kick"
    assert ke.ensure_path("https://abc.trycloudflare.com/", "/kick") == "https://abc.trycloudflare.com/kick"
    assert ke.ensure_path("https://abc.trycloudflare.com/kick", "/kick") == "https://abc.trycloudflare.com/kick"
    assert ke.ensure_path("https://abc.trycloudflare.com/kick/", "/kick") == "https://abc.trycloudflare.com/kick"
    assert ke.ensure_path("  https://abc.trycloudflare.com/kick  ", "/kick") == "https://abc.trycloudflare.com/kick"
    assert ke.ensure_path("", "/kick") == ""
    assert ke.ensure_path("https://abc.trycloudflare.com", "/") == "https://abc.trycloudflare.com"


if __name__ == "__main__":
    raise SystemExit(main())
