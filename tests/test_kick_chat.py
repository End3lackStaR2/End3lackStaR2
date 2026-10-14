#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ทดสอบออฟไลน์ของ kick_chat.py ด้วย Pusher server จำลอง (ไม่ต้องต่อเน็ต)

รัน:  python tests/test_kick_chat.py
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import socket
import struct
import sys
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import kick_chat as kc  # noqa: E402

GOOD_KEY = "34bf7a0ff419a2a775b9"        # key ปัจจุบันของ Kick (ต.ค. 2026)
RETIRED_KEY = "32cbd69e4b950bf97679"    # key เก่าที่ Kick ปิดแล้ว — ใช้ทดสอบเส้นทาง "ค่าตกยุค"
BAD_KEY = "ffffffffffffffffffff"


# ---------------------------------------------------------------------------
# Pusher server จำลอง (WebSocket + protocol 7)
# ---------------------------------------------------------------------------
class MockPusherServer(threading.Thread):
    """รับ connection เดียว/หลายตัว; key ที่ถูกต้อง = GOOD_KEY, cluster = us2"""

    def __init__(self, good_cluster: str = "us2", chatroom: int = 4242):
        super().__init__(daemon=True)
        self.good_cluster = good_cluster
        self.chatroom = chatroom
        self.received: list[dict] = []
        self._conns: list[socket.socket] = []
        self._lock = threading.Lock()
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()

    def run(self) -> None:
        self._srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass

    # --- WS helpers ---
    @staticmethod
    def _recv_until(sock: socket.socket, marker: bytes) -> bytes:
        buf = b""
        while marker not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("client ปิด")
            buf += chunk
        return buf

    @staticmethod
    def _send_text(sock: socket.socket, payload: str) -> None:
        data = payload.encode("utf-8")
        header = bytes([0x81])
        n = len(data)
        if n < 126:
            header += bytes([n])
        elif n < 65536:
            header += bytes([126]) + struct.pack("!H", n)
        else:
            header += bytes([127]) + struct.pack("!Q", n)
        sock.sendall(header + data)

    @staticmethod
    def _recv_text(sock: socket.socket) -> str | None:
        try:
            head = sock.recv(2)
        except socket.timeout:
            return None
        if len(head) < 2:
            return None
        opcode = head[0] & 0x0F
        masked = bool(head[1] & 0x80)
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", MockPusherServer._read(sock, 2))[0]
        elif length == 127:
            length = struct.unpack("!Q", MockPusherServer._read(sock, 8))[0]
        mask = MockPusherServer._read(sock, 4) if masked else b""
        payload = MockPusherServer._read(sock, length) if length else b""
        if masked:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        if opcode == 0x8:
            return None
        return payload.decode("utf-8", "replace")

    @staticmethod
    def _read(sock: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("client ปิด")
            buf += chunk
        return buf

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(5)
            request = self._recv_until(conn, b"\r\n\r\n").decode("latin-1", "replace")
            key = ""
            for line in request.split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            path = request.split(" ")[1] if " " in request else ""
            accept = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                ).encode()
            )

            import re

            m = re.search(r"/app/([0-9a-zA-Z]+)", path)
            app_key = m.group(1) if m else ""
            client_cluster = "us2" if "ws-us2" in request.lower() or "127.0.0.1" in request.lower() else "other"

            if app_key != GOOD_KEY or client_cluster != self.good_cluster:
                self._send_text(conn, json.dumps({
                    "event": "pusher:error",
                    "data": {"code": 4001, "message": f"App key {app_key} not in this cluster"},
                }))
                return

            self._send_text(conn, json.dumps({
                "event": "pusher:connection_established",
                "data": json.dumps({"socket_id": "123.456", "activity_timeout": 120}),
            }))
            with self._lock:
                self._conns.append(conn)

            while True:
                raw = self._recv_text(conn)
                if raw is None:
                    return
                event = json.loads(raw)
                self.received.append(event)
                name = event.get("event")
                if name == "pusher:subscribe":
                    channel = event["data"]["channel"]
                    if channel.startswith("chatrooms."):
                        self._send_text(conn, json.dumps({
                            "event": "pusher_internal:subscription_succeeded",
                            "data": json.dumps({"channel": channel}),
                        }))
                    else:
                        self._send_text(conn, json.dumps({
                            "event": "pusher_internal:subscription_error",
                            "data": json.dumps({"channel": channel, "error": "not allowed"}),
                        }))
                elif name == "pusher:ping":
                    self._send_text(conn, json.dumps({"event": "pusher:pong", "data": "{}"}))
        except (ConnectionError, OSError, json.JSONDecodeError):
            pass
        finally:
            with self._lock:
                if conn in self._conns:
                    self._conns.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def push_chat(self, username: str = "viewer1", content: str = "สวัสดีครับ"):
        """ส่ง ChatMessageEvent ไปยัง client ทุกตัวที่เชื่อมอยู่"""
        payload = {
            "id": "msg-1",
            "chatroom_id": self.chatroom,
            "content": content,
            "type": "message",
            "created_at": "2026-10-14T12:34:56Z",
            "sender": {
                "id": 555,
                "username": username,
                "slug": username.lower(),
                "identity": {"color": "#ff5733", "badges": [{"type": "moderator", "text": "Moderator"}]},
            },
        }
        self._push_event("App\\Events\\ChatMessageEvent", payload)

    def _push_event(self, name: str, data: dict) -> None:
        with self._lock:
            conns = list(self._conns)
        for conn in conns:
            try:
                self._send_text(conn, json.dumps({"event": name, "data": json.dumps(data)}))
            except OSError:
                pass


def make_listen_args(**over) -> argparse.Namespace:
    base = {"timeout": 5.0, "json": False, "verbose": False, "limit": 0,
            "no_reconnect": True, "max_backoff": 30.0, "channel": None, "chatroom": 4242}
    base.update(over)
    return argparse.Namespace(**base)


def make_probe_args(tmp, **over) -> argparse.Namespace:
    base = {"config": str(Path(tmp) / "kick_chat_config.json"), "app_key": None,
            "clusters": "us2,eu", "timeout": 5.0, "all": False, "ws_url": None}
    base.update(over)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------------------
# เทสต์
# ---------------------------------------------------------------------------
def test_build_and_parse_url() -> None:
    url = kc.build_ws_url("us2", GOOD_KEY)
    assert url.startswith("wss://ws-us2.pusher.com/app/" + GOOD_KEY)
    assert "protocol=7" in url and "flash=false" in url

    cluster, key, host = kc.parse_ws_url(
        f"wss://ws-us2.pusher.com/app/{GOOD_KEY}?protocol=7&client=js&version=8.4.0-rc2&flash=false"
    )
    assert cluster == "us2"
    assert key == GOOD_KEY
    assert host == "ws-us2.pusher.com"

    cluster2, key2, _ = kc.parse_ws_url("wss://ws-eu.pusher.com/app/abcdef0123456789?protocol=7")
    assert cluster2 == "eu" and key2 == "abcdef0123456789"


def test_extract_pusher_from_text() -> None:
    html = f'<script>window.pusher = "wss://ws-us2.pusher.com/app/{GOOD_KEY}?protocol=7";</script>'
    found = kc.extract_pusher_from_text(html)
    assert found == ("us2", GOOD_KEY)

    js = f'const opts={{key:"{GOOD_KEY}",cluster:"us2"}}'
    found2 = kc.extract_pusher_from_text(js)
    assert found2 and found2[1] == GOOD_KEY
    assert kc.extract_pusher_from_text("no pusher here") is None


def test_chat_message_parsing() -> None:
    payload = {
        "id": "abc",
        "content": "hello [emote:1:KEKW]",
        "created_at": "2026-10-14T01:02:03Z",
        "sender": {"id": 9, "username": "nueng", "slug": "nueng",
                   "identity": {"color": "#00ff00", "badges": [{"type": "subscriber"}]}},
    }
    msg = kc.chat_message_to_dict("App\\Events\\ChatMessageSentEvent", payload)
    assert msg and msg["username"] == "nueng" and msg["badges"] == ["subscriber"]
    assert kc.chat_message_to_dict("App\\Events\\ChatMessageEvent", payload) is not None
    assert kc.chat_message_to_dict("App\\Events\\FollowEvent", {"follower": {}}) is None
    line = kc.format_message(msg)
    assert "01:02:03" in line and "nueng: hello" in line


def test_websocket_handshake_and_pusher_flow() -> None:
    server = MockPusherServer()
    server.start()
    try:
        url = f"ws://127.0.0.1:{server.port}/app/{GOOD_KEY}?protocol=7&client=js&version=8.4.0-rc2&flash=false"
        client = kc.PusherClient(url, timeout=5.0, idle_ping=0.3)
        data = client.connect()
        assert data.get("socket_id") == "123.456"
        assert client.activity_timeout == 120

        client.subscribe("chatrooms.4242.v2")
        server.push_chat("viewer1", "สวัสดีครับ")
        got = None
        for name, payload in client.events():
            msg = kc.chat_message_to_dict(name, payload)
            if msg:
                got = msg
                break
        assert got and got["content"] == "สวัสดีครับ" and got["username"] == "viewer1"
        client.close()
    finally:
        server.stop()


def test_keepalive_ping_is_sent() -> None:
    server = MockPusherServer()
    server.start()
    try:
        url = f"ws://127.0.0.1:{server.port}/app/{GOOD_KEY}?protocol=7"
        client = kc.PusherClient(url, timeout=5.0, idle_ping=0.3)
        client.connect()
        client.subscribe("chatrooms.4242.v2")

        stop = threading.Event()

        def consume() -> None:
            try:
                for _name, _data in client.events():
                    if stop.is_set():
                        return
            except Exception:  # noqa: BLE001 - ปิด connection ตอนจบเทสต์
                pass

        threading.Thread(target=consume, daemon=True).start()
        time.sleep(1.6)
        stop.set()
        client.close()
        assert any(e.get("event") == "pusher:ping" for e in server.received), "ไม่พบ pusher:ping (keepalive)"
    finally:
        server.stop()


def test_wrong_cluster_error() -> None:
    server = MockPusherServer()
    server.start()
    try:
        url = f"ws://127.0.0.1:{server.port}/app/{BAD_KEY}?protocol=7"
        client = kc.PusherClient(url, timeout=5.0)
        try:
            client.connect()
            raise AssertionError("ควรโยน PusherWrongCluster")
        except kc.PusherWrongCluster as exc:
            assert "not in this cluster" in str(exc)
        finally:
            client.close()
    finally:
        server.stop()


def test_probe_classifies_clusters() -> None:
    server = MockPusherServer()
    server.start()
    try:
        good = f"ws://127.0.0.1:{server.port}/app/{GOOD_KEY}?protocol=7"
        bad_key = f"ws://127.0.0.1:{server.port}/app/{BAD_KEY}?protocol=7"
        dead = "ws://127.0.0.1:9/app/x?protocol=7"  # ปิดอยู่แน่นอน

        assert kc.probe_once(good, 3.0)[0] == "ok"
        assert kc.probe_once(bad_key, 3.0)[0] == "wrong-cluster"
        assert kc.probe_once(dead, 3.0)[0] in ("unreachable", "error")
    finally:
        server.stop()


def test_listen_session_streams_and_limits() -> None:
    server = MockPusherServer()
    server.start()
    try:
        url = f"ws://127.0.0.1:{server.port}/app/{GOOD_KEY}?protocol=7"
        args = make_listen_args(limit=1)
        out = io.StringIO()

        def push_soon():
            time.sleep(0.4)
            server.push_chat("nueng", "ทดสอบ")

        threading.Thread(target=push_soon, daemon=True).start()
        with redirect_stdout(out):
            rc = kc.listen_session(url, ["chatrooms.4242.v2", "chatrooms.4242"], args)
        assert rc == 0
        assert "nueng: ทดสอบ" in out.getvalue()
    finally:
        server.stop()


def test_listen_session_json_mode() -> None:
    server = MockPusherServer()
    server.start()
    try:
        url = f"ws://127.0.0.1:{server.port}/app/{GOOD_KEY}?protocol=7"
        args = make_listen_args(limit=1, json=True)
        out = io.StringIO()
        threading.Thread(target=lambda: (time.sleep(0.4), server.push_chat("a", "b")), daemon=True).start()
        with redirect_stdout(out):
            kc.listen_session(url, ["chatrooms.4242.v2"], args)
        line = [ln for ln in out.getvalue().strip().splitlines() if ln.startswith("{")][-1]
        parsed = json.loads(line)
        assert parsed["username"] == "a" and parsed["content"] == "b"
    finally:
        server.stop()


def test_config_roundtrip(tmp=None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "kick_chat_config.json"
        kc.save_config(path, {"cluster": "us2", "app_key": GOOD_KEY})
        kc.save_config(path, {"chatroom_id": 99})
        cfg = kc.load_config(path)
        assert cfg["cluster"] == "us2" and cfg["chatroom_id"] == 99

        args = argparse.Namespace(app_key=None, clusters=None)
        assert kc.app_keys_from(args, cfg)[0] == GOOD_KEY
        assert GOOD_KEY in kc.app_keys_from(args, {})
        assert kc.clusters_from(args, {"cluster": "us2"}) == ["us2"]
        assert kc.clusters_from(args, {})[:2] == ["us2", "us3"]


def test_cmd_probe_saves_winner(monkeypatch_target=None) -> None:
    import tempfile

    server = MockPusherServer()
    server.start()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "kick_chat_config.json"
            # ให้ probe ใช้ URL ของ server จำลองแทน host จริงของ Pusher
            original_build = kc.build_ws_url
            kc.build_ws_url = lambda cluster, key=kc.DEFAULT_APP_KEY: (
                f"ws://127.0.0.1:{server.port}/app/{key}?protocol=7" if cluster in ("us2", "eu")
                else f"ws://127.0.0.1:9/app/{key}?protocol=7"
            )
            try:
                args = make_probe_args(tmpdir, app_key=GOOD_KEY, clusters="us2,eu")
                out = io.StringIO()
                with redirect_stdout(out):
                    rc = kc.cmd_probe(args)
                assert rc == 0, out.getvalue()
                cfg = kc.load_config(cfg_path)
                assert cfg["cluster"] == "us2"
                assert cfg["app_key"] == GOOD_KEY
                assert '"channelTemplate": "chatrooms.{chatroom_id}.v2"' in out.getvalue()
            finally:
                kc.build_ws_url = original_build
    finally:
        server.stop()


def test_probe_failure_message() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        original_build = kc.build_ws_url
        kc.build_ws_url = lambda cluster, key=kc.DEFAULT_APP_KEY: f"ws://127.0.0.1:9/app/{key}?protocol=7"
        try:
            args = make_probe_args(tmpdir, app_key=GOOD_KEY, clusters="us2")
            out, err = io.StringIO(), io.StringIO()
            from contextlib import redirect_stderr

            with redirect_stdout(out), redirect_stderr(err):
                rc = kc.cmd_probe(args)
            combined = out.getvalue() + err.getvalue()
            assert rc == 1, combined
            assert "ไม่พบ cluster ที่ใช้ได้" in combined
            assert "DevTools" in combined
        finally:
            kc.build_ws_url = original_build


def test_cmd_discover_from_pasted_url() -> None:
    import tempfile

    server = MockPusherServer()
    server.start()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(
                config=str(Path(tmpdir) / "kick_chat_config.json"),
                app_key=None, clusters=None, timeout=2.0, all=False,
                ws_url=f"ws://127.0.0.1/app/{GOOD_KEY}?protocol=7", channel=None,
            )
            original_build = kc.build_ws_url
            kc.build_ws_url = lambda cluster, key=kc.DEFAULT_APP_KEY: (
                f"ws://127.0.0.1:{server.port}/app/{key}?protocol=7"
            )
            try:
                out = io.StringIO()
                with redirect_stdout(out):
                    kc.cmd_discover(args)
            finally:
                kc.build_ws_url = original_build
            cfg = kc.load_config(Path(tmpdir) / "kick_chat_config.json")
            assert cfg["app_key"] == GOOD_KEY
            assert cfg["cluster"] == "us2"
    finally:
        server.stop()


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

def test_extract_pusher_settings_shapes() -> None:
    # โครงสร้างแบบที่ 1: nest ใต้ pusher
    cfg1 = {"pusher": {"cluster": "us2", "appKey": GOOD_KEY,
                       "wsUrl": f"wss://ws-us2.pusher.com/app/{GOOD_KEY}?protocol=7"}}
    s1 = kc.extract_pusher_settings(cfg1)
    assert s1["cluster"] == "us2"
    assert s1["app_key"] == GOOD_KEY
    assert s1["ws_url"].startswith("wss://ws-us2.pusher.com/app/")
    assert kc.host_from_settings(s1) == "ws-us2.pusher.com"

    # โครงสร้างแบบที่ 2: แบน + ชื่อฟิลด์ต่างออกไป
    cfg2 = {"kick": {"chat": {"ws_host": "ws-eu.pusher.com", "app_key": BAD_KEY}}}
    s2 = kc.extract_pusher_settings(cfg2)
    assert s2["host"] == "ws-eu.pusher.com"
    assert s2["app_key"] == BAD_KEY
    assert kc.host_from_settings(s2) == "ws-eu.pusher.com"

    # โครงสร้างแบบที่ 3: มีแต่ appId ที่ไม่ใช่ hex ยาว ๆ ต้องไม่ถูกจับเป็น key
    s3 = kc.extract_pusher_settings({"appId": "123"})
    assert "app_key" not in s3

    # ไม่มีอะไรเลย
    assert "cluster" not in kc.extract_pusher_settings({"other": 1})


def test_patch_config_file_keeps_other_keys() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        path.write_text(json.dumps({
            "obs": {"scene": "main"},
            "pusher": {"cluster": "eu", "appKey": BAD_KEY, "wsUrl": "wss://ws-eu.pusher.com/app/x?protocol=7"},
            "other": [1, 2, 3],
        }, indent=2), encoding="utf-8")

        changes = kc.patch_config_file(path, {
            "cluster": "us2", "app_key": GOOD_KEY,
            "ws_url": f"wss://ws-us2.pusher.com/app/{GOOD_KEY}?protocol=7",
        })
        assert "appKey" in changes and "cluster" in changes
        assert any(str(k).startswith("_backup") for k in changes) or "_backup" in changes

        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["obs"] == {"scene": "main"}          # ข้อมูลอื่นต้องไม่หาย
        assert data["other"] == [1, 2, 3]
        assert data["pusher"]["cluster"] == "us2"
        assert data["pusher"]["appKey"] == GOOD_KEY
        assert "ws-us2" in data["pusher"]["wsUrl"]

        backups = list(Path(tmpdir).glob("*.bak-*"))
        assert backups, "ต้องมีไฟล์สำรอง"
        assert json.loads(backups[0].read_text(encoding="utf-8"))["pusher"]["cluster"] == "eu"

        # รันซ้ำด้วยค่าเดิม → ไม่มีการเปลี่ยนแปลง
        again = kc.patch_config_file(path, {
            "cluster": "us2", "app_key": GOOD_KEY,
            "ws_url": f"wss://ws-us2.pusher.com/app/{GOOD_KEY}?protocol=7",
        })
        assert again == {}


def test_patch_config_creates_pusher_block() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "config.json"
        path.write_text(json.dumps({"someOther": True}), encoding="utf-8")
        kc.patch_config_file(path, {"cluster": "us2", "app_key": GOOD_KEY}, backup=False)
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["someOther"] is True
        assert data["pusher"]["cluster"] == "us2"


def test_hosts_and_proxy_helpers() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        hosts = Path(tmpdir) / "hosts"
        hosts.write_text(
            "# comment\n"
            "127.0.0.1  localhost\n"
            "10.0.0.9   ws-us2.pusher.com   # แก้โดยโปรแกรมอะไรสักอย่าง\n",
            encoding="utf-8",
        )
        original = kc.HOSTS_FILES
        kc.HOSTS_FILES = (hosts,)
        try:
            hits = kc.hosts_file_hits("pusher")
            assert len(hits) == 1 and "ws-us2.pusher.com" in hits[0]
        finally:
            kc.HOSTS_FILES = original

        import os

        saved = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")}
        try:
            os.environ["HTTPS_PROXY"] = "http://127.0.0.1:8888"
            assert kc.proxy_env().get("HTTPS_PROXY") == "http://127.0.0.1:8888"
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            os.environ.pop("http_proxy", None)
            os.environ.pop("https_proxy", None)


def test_ip_classification() -> None:
    assert kc.is_private_ip("127.0.0.1")
    assert kc.is_private_ip("192.168.1.10")
    assert kc.is_private_ip("10.5.5.5")
    assert kc.is_private_ip("172.20.0.1")
    assert not kc.is_private_ip("18.140.1.1")
    assert not kc.is_private_ip("172.40.1.1")


def test_diagnose_verdict_all_branches() -> None:
    base = dict(configured_host="ws-us2.pusher.com", configured_key=GOOD_KEY, live_key=GOOD_KEY,
                live_cluster="us2", resolved_ips=["18.1.1.1"], hosts_hits=[], proxies={})

    summary, fixes = kc.diagnose_verdict(**base, handshake_ok=True, handshake_error="")
    assert "เชื่อมต่อ Pusher ได้แล้ว" in summary and fixes == []

    summary, fixes = kc.diagnose_verdict(**{**base, "configured_host": "ws-eu.pusher.com"},
                                         handshake_ok=False, handshake_error="App key X not in this cluster")
    assert "ต่อผิด cluster" in summary
    assert any("ws-us2.pusher.com" in f for f in fixes)

    summary, fixes = kc.diagnose_verdict(**{**base, "configured_key": BAD_KEY},
                                         handshake_ok=False, handshake_error="App key X not in this cluster")
    assert "ไม่ใช่ key ที่ Kick ใช้" in summary
    assert any("ไม่ตรงกับ key" in f for f in fixes)

    summary, fixes = kc.diagnose_verdict(**base, handshake_ok=False,
                                         handshake_error="App key X not in this cluster")
    assert "Kick หมุน key ล่าสุด" in summary
    assert fixes and "discover --ws-url" in fixes[0]

    summary, _ = kc.diagnose_verdict(**base, handshake_ok=False,
                                     handshake_error="Application does not exist")
    assert "ไม่ถูกต้องแล้ว" in summary

    summary, fixes = kc.diagnose_verdict(**{**base, "hosts_hits": ["C:/hosts: 10.0.0.9 ws-us2.pusher.com"],
                                            "proxies": {"HTTPS_PROXY": "x"},
                                            "resolved_ips": ["127.0.0.1"]},
                                         handshake_ok=False, handshake_error="หมดเวลาเชื่อมต่อ")
    assert any("hosts" in f for f in fixes)
    assert any("proxy env" in f for f in fixes)
    assert any("วงใน" in f for f in fixes)


def test_cmd_fix_config_applies(monkeypatch=None) -> None:
    import tempfile

    server = MockPusherServer()
    server.start()
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            cfg_path.write_text(json.dumps({
                "obs": {"scene": "keep-me"},
                "pusher": {"cluster": "eu", "appKey": BAD_KEY},
            }), encoding="utf-8")

            original_build = kc.build_ws_url
            kc.build_ws_url = lambda cluster, key=kc.DEFAULT_APP_KEY: (
                f"ws://127.0.0.1:{server.port}/app/{key}?protocol=7"
            )
            original_hosts = kc.HOSTS_FILES
            kc.HOSTS_FILES = (Path(tmpdir) / "no-such-hosts",)
            try:
                args = argparse.Namespace(
                    config=str(Path(tmpdir) / "chat_cfg.json"), config_file=str(cfg_path),
                    channel=None, ws_url=f"ws://127.0.0.1/app/{GOOD_KEY}?protocol=7",
                    apply=True, no_backup=False, compare=False, timeout=3.0,
                    app_key=None, clusters=None, all=False,
                )
                out, err = io.StringIO(), io.StringIO()
                from contextlib import redirect_stderr

                with redirect_stdout(out), redirect_stderr(err):
                    rc = kc.cmd_fix_config(args)
                combined = out.getvalue() + err.getvalue()
                assert rc == 0, combined
                assert "สรุป" in combined

                data = json.loads(cfg_path.read_text(encoding="utf-8"))
                assert data["obs"] == {"scene": "keep-me"}
                assert data["pusher"]["appKey"] == GOOD_KEY
                assert data["pusher"]["cluster"] == "us2"
                assert "127.0.0.1" in data["pusher"]["wsUrl"] or "us2" in data["pusher"]["wsUrl"]
                assert list(Path(tmpdir).glob("*.bak-*")), "ต้องมีไฟล์สำรอง"
            finally:
                kc.build_ws_url = original_build
                kc.HOSTS_FILES = original_hosts
    finally:
        server.stop()


def test_handshake_metadata_captured() -> None:
    server = MockPusherServer()
    server.start()
    try:
        url = f"ws://127.0.0.1:{server.port}/app/{GOOD_KEY}?protocol=7"
        client = kc.PusherClient(url, timeout=5.0)
        client.connect()
        assert client.handshake["host"] == "127.0.0.1"
        assert "101" in client.handshake["status"]
        assert any(k.lower() == "upgrade" for k in client.handshake["headers"])
        assert client.handshake["ips"], "ต้องบันทึก IP ที่ resolve ได้"
        client.close()

        bad = kc.PusherClient(f"ws://127.0.0.1:{server.port}/app/{BAD_KEY}?protocol=7", timeout=5.0)
        try:
            bad.connect()
        except kc.PusherWrongCluster:
            pass
        assert bad.last_error and "not in this cluster" in bad.last_error
        bad.close()
    finally:
        server.stop()


def test_scan_finds_and_flags_values() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        (root / "sub").mkdir()
        (root / "config.json").write_text(json.dumps({
            "pusher": {"cluster": "eu", "appKey": BAD_KEY,
                       "wsUrl": f"wss://ws-eu.pusher.com/app/{BAD_KEY}?protocol=7"},
        }), encoding="utf-8")
        (root / "sub" / "app.py").write_text(
            f'PUSHER_URL = "wss://ws-us2.pusher.com/app/{GOOD_KEY}?protocol=7"\n', encoding="utf-8")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "junk.js").write_text("wss://ws-ap1.pusher.com/app/" + BAD_KEY,
                                                       encoding="utf-8")
        (root / "binary.exe").write_bytes(b"\x00\x01pusher ws-eu.pusher.com")

        args = argparse.Namespace(path=tmpdir, exts=None, config=None, app_key=None, clusters=None,
                                 timeout=1.0, all=False, ws_url=None)
        out, err = io.StringIO(), io.StringIO()
        from contextlib import redirect_stderr

        with redirect_stdout(out), redirect_stderr(err):
            rc = kc.cmd_scan(args)
        text = out.getvalue()
        assert rc == 0
        assert "config.json:" in text and "ไม่ใช่ cluster ที่ Kick ใช้" in text
        assert "sub/app.py" in text and "ws-us2.pusher.com" in text
        assert "node_modules" not in text, "ต้องข้าม node_modules"
        assert "binary.exe" not in text, "ต้องข้ามไฟล์นามสกุลที่ไม่สนใจ"


def test_scan_reports_when_nothing_found() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "readme.md").write_text("nothing here about chat", encoding="utf-8")
        args = argparse.Namespace(path=tmpdir, exts=None, config=None, app_key=None, clusters=None,
                                  timeout=1.0, all=False, ws_url=None)
        out, err = io.StringIO(), io.StringIO()
        from contextlib import redirect_stderr

        with redirect_stdout(out), redirect_stderr(err):
            rc = kc.cmd_scan(args)
        assert rc == 1
        assert "ไม่พบค่า pusher" in err.getvalue()


def test_scan_text_for_pusher_dedupes() -> None:
    text = f"wss://ws-us2.pusher.com/app/{GOOD_KEY}?protocol=7\nwss://ws-us2.pusher.com/app/{GOOD_KEY}?protocol=7"
    hits = kc.scan_text_for_pusher(text)
    urls = [h for h in hits if h[0] == "ws-url"]
    assert len(urls) == 1, hits
    assert hits[0][1].startswith("wsUrl = ")


def test_extract_prefers_us2_host_over_key() -> None:
    text = (
        "Attempting: wss://ws-us2.pusher.com/app/0139bca5f0e1c1c7f4b0aa11?protocol=7\n"
        f"also: wss://ws-eu.pusher.com/app/{GOOD_KEY}?protocol=7\n"
        "cluster: us2\n"
    )
    guess = kc.best_pusher_guess(text)
    assert guess["cluster"] == "us2", guess
    assert "ws-us2" in guess["best_url"]
    assert guess["app_key"] == "0139bca5f0e1c1c7f4b0aa11"
    assert len(guess["urls_found"]) == 2


def test_extract_falls_back_to_public_key() -> None:
    guess = kc.best_pusher_guess("logging in... chatroom 999")
    assert guess["app_key"] == GOOD_KEY
    assert guess["cluster"] == "us2"
    assert guess["best_url"].startswith("wss://ws-us2.pusher.com/app/")


def test_cmd_extract_switches_to_working_canonical() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        cfg_path.write_text(json.dumps({"pusher": {"cluster": "us2", "appKey": BAD_KEY}}), encoding="utf-8")
        clip = f"ws://ws-us2.pusher.com/app/{BAD_KEY}?protocol=7\n"

        original_probe = kc.probe_once

        def fake_probe(url, timeout):
            return ("ok", "{}") if GOOD_KEY in url else ("wrong-cluster", "App key ... not in this cluster")

        kc.probe_once = fake_probe
        try:
            args = argparse.Namespace(
                config=None, app_key=None, clusters=None, timeout=2.0, all=False, ws_url=None,
                text=clip, file=None, probe=True, config_file=str(cfg_path), apply=True, no_backup=False,
            )
            out = io.StringIO()
            with redirect_stdout(out):
                rc = kc.cmd_extract(args)
            text = out.getvalue()
            assert rc == 0, text
            assert "ค่ามาตรฐานใช้ได้" in text

            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            assert data["pusher"]["appKey"] == GOOD_KEY, "ต้องสลับไปใช้ key ที่ต่อได้"
            assert data["pusher"]["cluster"] == "us2"
        finally:
            kc.probe_once = original_probe


def test_cmd_extract_keeps_found_values_when_they_work() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_path = Path(tmpdir) / "config.json"
        cfg_path.write_text("{}", encoding="utf-8")
        clip = f"ws://ws-us2.pusher.com/app/{GOOD_KEY}?protocol=7"

        original_probe = kc.probe_once
        kc.probe_once = lambda url, timeout: ("ok", "{}")
        try:
            args = argparse.Namespace(
                config=None, app_key=None, clusters=None, timeout=2.0, all=False, ws_url=None,
                text=clip, file=None, probe=True, config_file=str(cfg_path), apply=True, no_backup=False,
            )
            out = io.StringIO()
            with redirect_stdout(out):
                rc = kc.cmd_extract(args)
            assert rc == 0
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            assert data["pusher"]["appKey"] == GOOD_KEY
            assert "ค่ามาตรฐานใช้ได้" not in out.getvalue()
        finally:
            kc.probe_once = original_probe


def test_filter_ps_output() -> None:
    raw = "\n".join([
        "  111     1  00:05 /bin/bash -l -c cd /tmp && python3 kick_chat.py report",
        "  222   111  00:04 python3 /home/user/End3lackStaR2/kick_chat.py listen --chatroom 42",
        "  333     1  12:00 ps -eo pid,ppid,etime,cmd",
        "  444     1  03:00 /usr/bin/python3 /home/user/End3lackStaR2/kick_chat.py report --fast",
        "  555     1  00:01 grep python",
    ])
    filtered = kc._filter_ps_output(raw, own_pid=999)
    assert "listen --chatroom 42" in filtered
    assert "bash -l -c" not in filtered
    assert "ps -eo" not in filtered
    assert "kick_chat.py report" not in filtered
    assert kc._filter_ps_output("no python here", own_pid=1).startswith("(ไม่พบ process python")


def test_cmd_report_redacts_and_summarises() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        cfg_path = root / "kick_chat_config.json"
        cfg_path.write_text(json.dumps({
            "pusher": {"cluster": "us2", "appKey": BAD_KEY},
        }), encoding="utf-8")
        creds = root / "kick_credentials.json"
        creds.write_text(json.dumps({"client_id": GOOD_KEY, "client_secret": "s3cr3t-value-should-not-leak"}),
                         encoding="utf-8")
        tokens = root / "kick_tokens.json"
        tokens.write_text(json.dumps({"access_token": "tok-should-not-leak-123456", "refresh_token": "ref",
                                      "scope": "chat:write", "expires_at": int(time.time()) + 600}), encoding="utf-8")

        original = (kc.SCRIPT_DIR, kc.probe_once, kc.python_processes, kc.HOSTS_FILES)
        kc.SCRIPT_DIR = root
        kc.probe_once = lambda url, timeout: ("wrong-cluster", f"App key {BAD_KEY} not in this cluster")
        kc.python_processes = lambda: "1234  python.exe kick_chat.py listen --chatroom 42"
        kc.HOSTS_FILES = (root / "no-hosts",)
        try:
            args = argparse.Namespace(config=str(cfg_path), tokens=str(tokens), fast=True, show_keys=False,
                                     timeout=2.0, app_key=None, clusters=None, all=False, ws_url=None)
            out, err = io.StringIO(), io.StringIO()
            from contextlib import redirect_stderr

            with redirect_stdout(out), redirect_stderr(err):
                rc = kc.cmd_report(args)
            text = out.getvalue()
            assert rc == 0
            assert "KICK CHAT REPORT" in text
            assert "chat.message" not in text  # ไม่ควรมีข้อมูลแปลกปลอม
            assert "s3cr3t-value-should-not-leak" not in text, "client secret ต้องถูกปิด"
            assert "tok-should-not-leak-123456" not in text, "access token ต้องถูกปิด"
            assert "scope       : chat:write" in text
            assert "listen --chatroom 42" in text, "ต้องเห็น process ที่ค้างอยู่"
            assert f"{BAD_KEY[:8]}..." in text, "key ต้องถูกย่อ"
            assert "not in this cluster" in text, "ต้องเห็นข้อความ error จาก Pusher"
            assert "OAuth client_id" in text or "key ข้างบน" in text
        finally:
            kc.SCRIPT_DIR, kc.probe_once, kc.python_processes, kc.HOSTS_FILES = original


def test_cmd_report_show_keys_reveals() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        cfg_path = root / "kick_chat_config.json"
        cfg_path.write_text(json.dumps({"cluster": "us2", "appKey": GOOD_KEY}), encoding="utf-8")
        original = (kc.SCRIPT_DIR, kc.probe_once, kc.python_processes, kc.HOSTS_FILES)
        kc.SCRIPT_DIR = root
        kc.probe_once = lambda url, timeout: ("ok", "{}")
        kc.python_processes = lambda: "(ไม่มี)"
        kc.HOSTS_FILES = (root / "no-hosts",)
        try:
            args = argparse.Namespace(config=str(cfg_path), tokens=str(root / "none.json"), fast=True,
                                     show_keys=True, timeout=2.0, app_key=None, clusters=None, all=False, ws_url=None)
            out = io.StringIO()
            from contextlib import redirect_stderr

            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                kc.cmd_report(args)
            assert GOOD_KEY in out.getvalue()
        finally:
            kc.SCRIPT_DIR, kc.probe_once, kc.python_processes, kc.HOSTS_FILES = original


if __name__ == "__main__":
    raise SystemExit(main())
