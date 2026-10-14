#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ทดสอบออฟไลน์ของ tools/run_and_report.py (ไม่ต้องต่อเน็ต ไม่ต้องมี Kick)

รัน:  python tests/test_run_and_report.py

ตรวจว่า: รวมผล 3 ขั้นเป็นไฟล์เดียวได้, ตรวจเจอ key เก่า + log ที่ยังไม่ต่อติด,
ไม่สแกนไฟล์รายงานของตัวเอง, และคืนรหัสออกที่ถูกต้อง (0 = ผ่าน, 1 = มีที่ต้องแก้)
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(REPO_DIR / "tools"))

import run_and_report as rar  # noqa: E402
import ws_probe as wp  # noqa: E402

CURRENT = wp.DEFAULT_KEY
RETIRED = wp.RETIRED_KEYS[0]


def _make_tree(tmp: Path, *, config_key: str, log_text: str, extra_files: dict | None = None) -> Path:
    (tmp / "files").mkdir(parents=True, exist_ok=True)
    (tmp / "files" / "config.json").write_text(
        json.dumps({"pusher": {"cluster": "us2", "appKey": config_key}}), encoding="utf-8")
    (tmp / "files" / "kick_notifier.log").write_text(log_text, encoding="utf-8")
    for name, content in (extra_files or {}).items():
        (tmp / name).write_text(content, encoding="utf-8")
    return tmp


def _run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = rar.main(argv)
    return code, out.getvalue()


def test_all_good_reports_success() -> None:
    with tempfile.TemporaryDirectory() as tmpname:
        tmp = Path(tmpname)
        _make_tree(tmp, config_key=CURRENT,
                   log_text=f'[20:20:01] ws://ws-us2.pusher.com/app/{CURRENT}\n'
                            '[20:20:02] Frame event=pusher:connection_established data={"socket_id":"42.1"}\n')
        report_file = tmp / "out.txt"
        code, text = _run(["--root", str(tmp), "--no-probe", "--out", str(report_file), "--quiet"])
        assert code == 0, text
        assert report_file.is_file()
        body = report_file.read_text(encoding="utf-8")
        assert "pusher:connection_established" in body
        assert "✅ ทุกขั้นผ่าน" in body
        # ขั้นที่ 2 ต้องไม่สแกนไฟล์รายงานของตัวเอง (บรรทัดผลพบจะอยู่ในรูป path:line:)
        assert "out.txt:1:" not in body


def test_old_key_and_stale_log_are_reported() -> None:
    with tempfile.TemporaryDirectory() as tmpname:
        tmp = Path(tmpname)
        _make_tree(tmp, config_key=CURRENT, log_text="[20:05:01] เริ่มโปรแกรม\n",
                   extra_files={"boot.py": f'KEY = "{RETIRED}"\n'})
        report_file = tmp / "report.txt"
        code, _text = _run(["--root", str(tmp), "--no-probe", "--out", str(report_file), "--quiet"])
        assert code == 1
        body = report_file.read_text(encoding="utf-8")
        assert "boot.py" in body, body
        assert "ถูก Kick ปิดแล้ว" in body
        assert "ยังมีจุดที่ต้องแก้" in body


def test_old_config_is_flagged() -> None:
    with tempfile.TemporaryDirectory() as tmpname:
        tmp = Path(tmpname)
        _make_tree(tmp, config_key=RETIRED,
                   log_text='[20:20:02] Frame event=pusher:connection_established data={"socket_id":"7.7"}\n')
        report_file = tmp / "r.txt"
        code, _text = _run(["--root", str(tmp), "--no-probe", "--out", str(report_file), "--quiet"])
        assert code == 1
        body = report_file.read_text(encoding="utf-8")
        assert "key เก่าที่ Kick ปิดแล้ว" in body  # จากขั้นที่ 3 (--config)


def test_missing_log_is_not_fatal() -> None:
    with tempfile.TemporaryDirectory() as tmpname:
        tmp = Path(tmpname)
        (tmp / "files").mkdir(parents=True)
        (tmp / "files" / "config.json").write_text(json.dumps({"appKey": CURRENT}), encoding="utf-8")
        report_file = tmp / "r.txt"
        code, _text = _run(["--root", str(tmp), "--no-probe", "--out", str(report_file), "--quiet"])
        body = report_file.read_text(encoding="utf-8")
        assert "ไม่พบไฟล์ log" in body, body
        assert code == 1, "ไม่มีหลักฐานว่าต่อติดได้ ก็ไม่ควรถือว่าผ่าน"


def test_no_probe_skips_network_step() -> None:
    with tempfile.TemporaryDirectory() as tmpname:
        tmp = Path(tmpname)
        _make_tree(tmp, config_key=CURRENT,
                   log_text='[20:20:02] event=pusher:connection_established\n')
        report_file = tmp / "r.txt"
        code, _text = _run(["--root", str(tmp), "--no-probe", "--out", str(report_file), "--quiet"])
        body = report_file.read_text(encoding="utf-8")
        assert "ทดสอบต่อ Pusher จริง" not in body
        assert code == 0, body


def test_environment_context_has_expected_keys() -> None:
    lines = rar.environment_context()
    joined = "\n".join(lines)
    assert "process python" in joined
    assert "proxy env" in joined
    assert "hosts file" in joined


def test_default_root_prefers_existing_folder() -> None:
    assert rar.default_root("/tmp") == Path("/tmp")
    # ไม่ระบุ → ต้องได้ path ที่เป็น Path เสมอ (ไม่โยน exception แม้ไม่มี D:\HermesAI\OBS)
    assert isinstance(rar.default_root(None), Path)


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


if __name__ == "__main__":
    raise SystemExit(main())
