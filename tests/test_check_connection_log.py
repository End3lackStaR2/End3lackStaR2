#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ทดสอบออฟไลน์ของ tools/check_connection_log.py (ไม่ต้องมี log จริง ไม่ต้องต่อเน็ต)

รัน:  python tests/test_check_connection_log.py

จำลอง log แบบต่าง ๆ (ต่อติด / ถูกปฏิเสธด้วย key เก่า / ถูกปฏิเสธด้วย key อื่น /
TLS ถูกตัด / log นิ่ง) แล้วตรวจว่าผลตรวจและรหัสออกถูกต้อง
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(REPO_DIR / "tools"))

import check_connection_log as ccl  # noqa: E402
import ws_probe as wp  # noqa: E402

CURRENT = wp.DEFAULT_KEY
RETIRED = wp.RETIRED_KEYS[0]

LOG_OK = f"""[2026-10-14 20:20:01] เชื่อมต่อ wss://ws-us2.pusher.com/app/{CURRENT}?protocol=7
[2026-10-14 20:20:01] WS HTTP/1.1 101 Switching Protocols
[2026-10-14 20:20:01] Frame event=pusher:connection_established data={{"socket_id":"222.333.444","activity_timeout":120}}
[2026-10-14 20:20:02] subscribed chatrooms.{121539855}.v2
"""

LOG_RETIRED = f"""[2026-10-14 20:20:01] เชื่อมต่อ wss://ws-us2.pusher.com/app/{RETIRED}?protocol=7
[2026-10-14 20:20:02] Pusher : error {{"code":4001,"message":"App key {RETIRED} not in this cluster"}}
"""

LOG_OTHER_KEY = """[2026-10-14 20:20:02] Pusher : error {"code":4001,"message":"App key 01M2WEYN058CXSA5EQB2QATV8M not in this cluster"}
"""

LOG_TLS = """[2026-10-14 20:20:01] SSLError: TLS/SSL connection has been closed (EOF) (_ssl.c:992)
"""

LOG_EMPTY = """[2026-10-14 20:20:01] เริ่มโปรแกรม
"""


def _write(tmp: Path, name: str, text: str) -> Path:
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return path


def _run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = ccl.main(argv)
    return code, out.getvalue()


def test_analyze_connected() -> None:
    info = ccl.analyze(LOG_OK, expect_key=CURRENT)
    assert info["established"] is True
    assert info["handshake_ok"] is True
    assert info["socket_id"] == "222.333.444"
    assert info["refused"] is False
    status, detail, code = ccl.verdict_for(info, CURRENT, stale_minutes=None)
    assert (status, code) == ("connected", 0), (status, detail, code)


def test_analyze_retired_key() -> None:
    info = ccl.analyze(LOG_RETIRED, expect_key=CURRENT)
    assert info["refused"] is True
    assert info["keys_seen"] == [RETIRED]
    assert RETIRED in info["retired_in_log"]
    status, detail, code = ccl.verdict_for(info, CURRENT, stale_minutes=None)
    assert status == "refused" and code == 3
    assert "key เก่าที่ Kick ปิดแล้ว" in detail
    assert "สตาร์ทใหม่" in detail  # ต้องบอกวิธีแก้ ไม่ใช่แค่บอกว่าผิด


def test_analyze_unknown_key_points_to_override() -> None:
    info = ccl.analyze(LOG_OTHER_KEY, expect_key=CURRENT)
    assert info["keys_seen"] == ["01M2WEYN058CXSA5EQB2QATV8M"]
    status, detail, code = ccl.verdict_for(info, CURRENT, stale_minutes=None)
    assert status == "refused" and code == 3
    assert "override" in detail


def test_transport_and_stale_verdicts() -> None:
    tls_info = ccl.analyze(LOG_TLS, expect_key=CURRENT)
    assert any(label == "TLS" for label, _ in tls_info["transport"])
    status, _detail, code = ccl.verdict_for(tls_info, CURRENT, stale_minutes=None)
    assert status == "transport" and code == 4

    quiet = ccl.analyze(LOG_EMPTY, expect_key=CURRENT)
    status, detail, code = ccl.verdict_for(quiet, CURRENT, stale_minutes=120)
    assert status == "stale" and code == 4
    assert "120 นาที" in detail


def test_cli_on_files() -> None:
    with tempfile.TemporaryDirectory() as tmpname:
        tmp = Path(tmpname)

        ok_path = _write(tmp, "kick_notifier.log", LOG_OK)
        code, text = _run([str(ok_path), "--stale-minutes", "0"])
        assert code == 0, text
        assert "pusher:connection_established" in text
        assert "socket_id=222.333.444" in text

        retired_path = _write(tmp, "retired.log", LOG_RETIRED)
        code, text = _run([str(retired_path), "--stale-minutes", "0"])
        assert code == 3, text
        assert RETIRED in text

        # ค้นหาไฟล์ log ให้เองในโฟลเดอร์ (ใช้ชื่อ kick_notifier.log)
        import os

        cwd = Path.cwd()
        try:
            os.chdir(tmp)
            code, text = _run(["--stale-minutes", "0"])
            assert code == 0, text
            assert "kick_notifier.log" in text
        finally:
            os.chdir(cwd)


def test_cli_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmpname:
        code, text = _run([str(Path(tmpname) / "ไม่มีไฟล์นี้.log")])
        assert code == 5, text
        assert "ไม่พบไฟล์ log" in text


def test_config_key_check() -> None:
    with tempfile.TemporaryDirectory() as tmpname:
        tmp = Path(tmpname)
        old_cfg = tmp / "config.json"
        old_cfg.write_text(json.dumps({"pusher": {"cluster": "us2", "appKey": RETIRED}}), encoding="utf-8")
        key, note = ccl.check_config_key(old_cfg)
        assert key == RETIRED
        assert "ปิดแล้ว" in note
        assert "us2" in note

        new_cfg = tmp / "config_new.json"
        new_cfg.write_text(json.dumps({"pusher": {"cluster": "us2", "appKey": CURRENT}}), encoding="utf-8")
        key, note = ccl.check_config_key(new_cfg)
        assert key == CURRENT
        assert "ปัจจุบัน" in note

        broken = tmp / "broken.json"
        broken.write_text("{ นี่ไม่ใช่ json", encoding="utf-8")
        key, note = ccl.check_config_key(broken)
        assert key == "" and note, note


def test_cli_flags_old_config() -> None:
    with tempfile.TemporaryDirectory() as tmpname:
        tmp = Path(tmpname)
        log = _write(tmp, "notifier.log", LOG_OK)
        cfg = tmp / "config.json"
        cfg.write_text(json.dumps({"pusher": {"cluster": "us2", "appKey": RETIRED}}), encoding="utf-8")
        code, text = _run([str(log), "--config", str(cfg), "--stale-minutes", "0"])
        assert code == 0, text
        assert "config นี้ยังเป็น key เก่า" in text, text
        assert "สตาร์ท" in text or "ปฏิเสธ" in text


def test_follow_picks_up_new_lines() -> None:
    """จำลอง process ที่เพิ่งต่อติดระหว่างเฝ้าดู — ต้องรายงานว่าต่อติด"""
    with tempfile.TemporaryDirectory() as tmpname:
        tmp = Path(tmpname)
        path = _write(tmp, "kick_notifier.log", "[20:00:00] เริ่มโปรแกรม\n")
        import threading

        def append_later() -> None:
            time.sleep(0.8)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(f"[20:00:03] Frame event=pusher:connection_established data={{\"socket_id\":\"777.1\"}}\n")

        thread = threading.Thread(target=append_later)
        thread.start()
        code, text = _run([str(path), "--follow", "6", "--stale-minutes", "0"])
        thread.join(timeout=5)
        assert code == 0, text
        assert "socket_id=777.1" in text, text


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"[PASS] {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"[FAIL] {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"[ERROR] {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


def test_decode_bytes_handles_windows_encodings() -> None:
    """log บน Windows อาจเป็น UTF-16 (PowerShell) หรือ cp1252 (open() เปล่า ๆ)"""
    body = f"[20:00:02] ต่อ wss://ws-us2.pusher.com/app/{CURRENT}\r\n" \
           "[20:00:03] Frame event=pusher:connection_established data={\"socket_id\":\"1.2\"}\r\n"

    assert "pusher:connection_established" in ccl.decode_bytes(body.encode("utf-16"))
    assert "pusher:connection_established" in ccl.decode_bytes(body.encode("utf-8-sig"))
    assert "pusher:connection_established" in ccl.decode_bytes(body.encode("utf-8"))
    # cp1252 เขียนThaiไม่ได้ครบ แต่ต้องไม่พังและยังจับ key ได้
    legacy = ccl.decode_bytes(f"[20:00:02] key={RETIRED}\r\n".encode("cp1252"))
    assert RETIRED in legacy
    assert ccl.decode_bytes(b"") == "" or ccl.decode_bytes(b"") == ""


def test_read_tail_on_real_files(tmp_path=None) -> None:
    with tempfile.TemporaryDirectory() as tmpname:
        tmp = Path(tmpname)
        utf16 = tmp / "utf16.log"
        utf16.write_bytes(
            f"[20:00:03] Frame event=pusher:connection_established data={{\"socket_id\":\"9.9\"}}\r\n"
            .encode("utf-16"))
        info = ccl.analyze(ccl.read_tail(utf16), expect_key=CURRENT)
        assert info["established"] and info["socket_id"] == "9.9"

        empty = tmp / "empty.log"
        empty.write_bytes(b"")
        assert ccl.read_tail(empty) == ""


def test_old_key_in_log_without_refusal() -> None:
    """log ที่ยังพิมพ์ URL ด้วย key เก่า (ไม่มีบรรทัด 4001) ต้องถูกจับเป็น 'ใช้ค่าเก่า'"""
    text = f"[20:00:01] ต่อ wss://ws-us2.pusher.com/app/{RETIRED}?protocol=7\r\n[20:00:02] กำลังเชื่อมต่อ...\r\n"
    info = ccl.analyze(text, expect_key=CURRENT)
    assert info["retired_in_log"] == [RETIRED]
    assert info["established"] is False and info["refused"] is False
    status, detail, code = ccl.verdict_for(info, CURRENT, stale_minutes=None)
    assert status == "old_key" and code == 3, (status, detail, code)
    assert "ใช้ค่าเก่า" in detail


def test_cli_old_key_verdict_exit_code() -> None:
    with tempfile.TemporaryDirectory() as tmpname:
        path = Path(tmpname) / "notifier.log"
        path.write_text(f"[20:00:01] ws://ws-us2.pusher.com/app/{RETIRED}\n", encoding="utf-8")
        code, text = _run([str(path), "--stale-minutes", "0"])
        assert code == 3, text
        assert "key เก่าที่ Kick ปิดแล้ว" in text

if __name__ == "__main__":
    raise SystemExit(main())
