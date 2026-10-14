#!/usr/bin/env python3
"""รันเทสต์ทั้งหมดของโปรเจกต์นี้ในคำสั่งเดียว

    python tests/run_all.py

- ชุด Python (ไม่ต้องติดตั้งอะไรเพิ่ม) : test_kick_chat.py, test_kick_oauth.py, test_ws_probe_events.py
- ชุด JavaScript (ต้องมี Node.js)      : test_kick_probe_html.js, test_overlay_html.js
  ถ้าเครื่องไม่มี Node จะ "ข้าม" ให้ ไม่ถือว่าไม่ผ่าน

ทางเลือก: `python tests/run_all.py --python-only` สำหรับเครื่องที่ไม่มี Node
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PY_SUITES = ("test_kick_chat.py", "test_kick_oauth.py", "test_ws_probe_events.py")
JS_SUITES = ("test_kick_probe_html.js", "test_overlay_html.js")


def run_python_suite(name: str) -> tuple[bool, str]:
    path = TESTS_DIR / name
    proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, cwd=str(TESTS_DIR.parent))
    tail = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    summary = tail[-1] if tail else "(ไม่มีผลลัพธ์)"
    failures = [line for line in tail if line.startswith(("[FAIL]", "[ERROR]"))]
    for line in failures:
        print("    " + line)
    return proc.returncode == 0, summary


def run_js_suite(name: str) -> tuple[bool | None, str]:
    path = TESTS_DIR / name
    try:
        proc = subprocess.run(["node", str(path)], capture_output=True, text=True, cwd=str(TESTS_DIR.parent))
    except FileNotFoundError:
        return None, "ข้าม (ไม่พบ Node.js)"
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    for line in lines:
        if line.startswith("❌"):
            print("    " + line)
    summary = lines[-1] if lines else "(ไม่มีผลลัพธ์)"
    return proc.returncode == 0, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="รันเทสต์ทั้งหมดของ End3lackStaR2")
    parser.add_argument("--python-only", action="store_true", help="ข้ามเทสต์ JavaScript")
    args = parser.parse_args(argv)

    print(f"Python: {sys.version.split()[0]}  ({sys.executable})\n")
    results: list[tuple[str, bool | None, str]] = []

    for name in PY_SUITES:
        print(f"[python] {name}")
        passed, summary = run_python_suite(name)
        print(f"    -> {summary}")
        results.append((name, passed, summary))

    if not args.python_only:
        for name in JS_SUITES:
            print(f"[node]   {name}")
            passed, summary = run_js_suite(name)
            print(f"    -> {summary}")
            results.append((name, passed, summary))

    failed = [name for name, passed, _ in results if passed is False]
    skipped = [name for name, passed, _ in results if passed is None]
    print("\n" + "=" * 60)
    print(f"ผ่าน {sum(1 for _, p, _ in results if p is True)}/{len(results)} ชุด"
          + (f" · ข้าม {len(skipped)}" if skipped else "")
          + (f" · ไม่ผ่าน {len(failed)}: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
