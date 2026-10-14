#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_and_report.py — รันตรวจทั้งชุดแล้วรวมผลเป็นไฟล์เดียว (สำหรับส่งกลับมาดู)

ทำไมต้องมี: เวลาต้องบอกว่า "ติดตรงไหน" การส่งข้อความ 3-4 ท่อนจากคำสั่งต่างกันทำให้ตามไม่ทัน
สคริปต์นี้รันทุกอย่างที่จำเป็นแล้วเขียนเป็นไฟล์สรุปไฟล์เดียว (ค่าเริ่มต้น kick_report_<วันเวลา>.txt)

สิ่งที่รัน (ข้ามได้ด้วย flag):
  1) ตรวจ key + เส้นทางไป Pusher  → ws_probe.py  (ถาม Kick ด้วยถ้าทำได้)
  2) ตรวจไฟล์ config/โค้ดทั้งโฟลเดอร์ว่ามี key เก่าค้างไหม → kick_chat.py scan
  3) ตรวจ log ของโปรแกรมแจ้งเตือนว่าต่อติดหรือยัง → check_connection_log.py
  4) ข้อมูลบริบท: process python ที่รันอยู่ + proxy env + hosts file (เฉพาะที่เกี่ยวกับ pusher)

รัน:
    python tools\\run_and_report.py
    python tools\\run_and_report.py --root "D:\\HermesAI\\OBS" --log "D:\\HermesAI\\OBS\\files\\kick_notifier.log" --config "D:\\HermesAI\\OBS\\files\\config.json"
    python tools\\run_and_report.py --no-probe          # เครื่องที่เน็ตไม่ถึง pusher/kick เลย
    python tools\\run_and_report.py --quiet             # พิมพ์แค่สรุปท้าย (รายละเอียดอยู่ในไฟล์)

รหัสออก: 0 = ทุกอย่างดูดี · 1 = มีอย่างน้อยหนึ่งขั้นที่ต้องแก้ · 2 = ใช้คำสั่งผิด
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = REPO_DIR / "tools"
for extra in (REPO_DIR, TOOLS_DIR):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import check_connection_log as ccl  # noqa: E402
import kick_chat as kc  # noqa: E402
import ws_probe as wp  # noqa: E402

DEFAULT_ROOTS = ("D:\\HermesAI\\OBS", "D:/HermesAI/OBS")
LOG_CANDIDATES = ("kick_notifier.log", "notifier.log", "kick_chat.log")
CONFIG_CANDIDATES = ("config.json", "kick_chat_config.json", "settings.json")


class Report:
    """รวมผลทุกลงใน buffer เดียว แล้วเขียนทั้งไฟล์และ stdout"""

    def __init__(self, echo: bool = True) -> None:
        self.buffer = io.StringIO()
        self.echo = echo

    def write(self, text: str = "") -> None:
        self.buffer.write(text + "\n")
        if self.echo:
            print(text, flush=True)

    def section(self, title: str) -> None:
        self.write("")
        self.write("=" * 74)
        self.write(title)
        self.write("=" * 74)

    def capture(self, func, *args, **kwargs) -> tuple[int, str]:
        """รันฟังก์ชันที่พิมพ์ออก stdout แล้วเก็บข้อความไว้ในรายงาน"""
        buffer = io.StringIO()
        errors = io.StringIO()
        try:
            with redirect_stdout(buffer), redirect_stderr(errors):
                code = func(*args, **kwargs)
        except SystemExit as exc:  # บางคำสั่งเรียก sys.exit เอง
            code = int(exc.code or 0)
        except Exception as exc:  # noqa: BLE001 — รายงานทุกความล้มเหลว ไม่ให้สคริปต์ตาย
            code = 99
            buffer.write(f"(รันไม่สำเร็จ: {type(exc).__name__}: {exc})\n")
        text = buffer.getvalue() + errors.getvalue()
        self.write(text.rstrip())
        return int(code or 0), text

    def save(self, path: Path) -> None:
        path.write_text(self.buffer.getvalue(), encoding="utf-8")


def find_first(root: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def default_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    for candidate in DEFAULT_ROOTS:
        path = Path(candidate)
        if path.is_dir():
            return path
    return Path.cwd()


def environment_context() -> list[str]:
    """ข้อมูลบริบทที่ช่วยวินิจฉัย: process ที่รันอยู่, proxy, hosts file"""
    lines: list[str] = []
    # kc.python_processes() คืนเป็นข้อความสำเร็จรูป (tasklist + command line ทุกบรรทัด)
    processes = kc.python_processes()
    lines.append("process python ที่รันอยู่:")
    for line in str(processes).splitlines():
        lines.append("  " + line.rstrip()[:160])
    if "ไม่พบ process" not in str(processes):
        lines.append("  ⚠️ ถ้ามีมากกว่า 1 ตัว และมีตัวที่เริ่มก่อนแก้ config → ตัวนั้นยังใช้ค่าเก่า ต้องปิดให้หมด")

    proxies = kc.proxy_env()
    lines.append("proxy env: " + (", ".join(f"{k}={v}" for k, v in proxies.items()) if proxies else "ไม่มี"))

    try:
        hits = kc.hosts_file_hits("pusher")
    except Exception as exc:  # noqa: BLE001
        hits = [f"(อ่าน hosts file ไม่ได้: {type(exc).__name__})"]
    lines.append("hosts file (pusher): " + ("; ".join(hits) if hits else "ไม่มีการแก้ไข"))
    return lines


def main(argv: list[str] | None = None) -> int:
    wp._fix_stdout()
    parser = argparse.ArgumentParser(
        prog="run_and_report.py",
        description="รันตรวจทั้งชุด (key/เส้นทาง, ไฟล์ที่มี key เก่า, log) แล้วรวมเป็นไฟล์เดียว",
    )
    parser.add_argument("--root", help="โฟลเดอร์โปรเจกต์ที่ต้องการสแกน (ค่าเริ่มต้น D:\\HermesAI\\OBS ถ้ามี)")
    parser.add_argument("--log", help="ไฟล์ log ของโปรแกรมแจ้งเตือน")
    parser.add_argument("--config", help="ไฟล์ config ที่โปรแกรมอ่าน (สำหรับตรวจ key)")
    parser.add_argument("--out", help="ชื่อไฟล์รายงาน (ค่าเริ่มต้น kick_report_<วันเวลา>.txt)")
    parser.add_argument("--no-probe", action="store_true", help="ข้ามขั้นทดสอบต่อ Pusher จริง")
    parser.add_argument("--no-scan", action="store_true", help="ข้ามขั้นสแกนไฟล์")
    parser.add_argument("--no-log", action="store_true", help="ข้ามขั้นตรวจ log")
    parser.add_argument("--timeout", type=float, default=8.0, help="หมดเวลาต่อการเชื่อมต่อ (วินาที)")
    parser.add_argument("--follow", type=float, default=0, help="เฝ้าดู log เพิ่มอีกกี่วินาที")
    parser.add_argument("--quiet", action="store_true", help="พิมพ์แค่สรุปท้าย (รายละเอียดอยู่ในไฟล์)")
    args = parser.parse_args(argv)

    report = Report(echo=not args.quiet)
    root = default_root(args.root)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = Path(args.out) if args.out else Path.cwd() / f"kick_report_{stamp}.txt"

    report.section("สรุปการตรวจ Kick chat / notifier")
    report.write(f"เวลา       : {time.strftime('%Y-%m-%d %H:%M:%S')}")
    report.write(f"โฟลเดอร์    : {root}")
    report.write(f"Python     : {sys.version.split()[0]} ({sys.executable})")
    report.write(f"key ที่ควรเป็น: {wp.DEFAULT_KEY} · cluster {wp.DEFAULT_CLUSTER}")
    report.write(f"key ที่ถูกปิดแล้ว: {', '.join(wp.RETIRED_KEYS)}")

    problems: list[str] = []

    # ---------- 1) ทดสอบเส้นทาง + key ----------
    if not args.no_probe:
        report.section("1) ทดสอบต่อ Pusher จริงด้วย key ปัจจุบัน (ws_probe.py)")
        code, text = report.capture(wp.main, ["--timeout", str(args.timeout)])
        if code == 0:
            report.write("[ผลขั้น 1] ✅ ต่อได้ — key/host ถูกต้อง")
        else:
            report.write(f"[ผลขั้น 1] ❌ ยังไม่ผ่าน (รหัส {code}) — ดูรายละเอียดด้านบน")
            problems.append(f"ws_probe คืนรหัส {code}")

    # ---------- 2) สแกนหา key เก่าในไฟล์ ----------
    if not args.no_scan:
        report.section(f"2) สแกนหา key เก่า/ค่าผิดใน {root} (kick_chat.py scan)")
        if not root.is_dir():
            report.write(f"[ข้าม] ไม่พบโฟลเดอร์ {root} — ระบุใหม่ด้วย --root")
        else:
            scan_args = argparse.Namespace(path=str(root), exts=None, config=None, app_key=None,
                                           clusters=None, timeout=args.timeout, all=False, ws_url=None,
                                           exclude=out_path.name)
            code, _text = report.capture(kc.cmd_scan, scan_args)
            if code == 0:
                report.write("[ผลขั้น 2] ✅ ไม่พบค่าที่ต้องแก้")
            else:
                report.write("[ผลขั้น 2] ❌ พบค่าที่ต้องแก้ (ดูรายการไฟล์ด้านบน)")
                problems.append("พบไฟล์ที่มี key เก่า/ค่าที่ต้องแก้")

    # ---------- 3) ตรวจ log ----------
    if not args.no_log:
        report.section("3) ตรวจ log ของโปรแกรมแจ้งเตือน (check_connection_log.py)")
        log_path = Path(args.log).expanduser() if args.log else (find_first(root, LOG_CANDIDATES) or find_first(root / "files", LOG_CANDIDATES))
        config_path = Path(args.config).expanduser() if args.config else (find_first(root, CONFIG_CANDIDATES) or find_first(root / "files", CONFIG_CANDIDATES))
        if log_path is None:
            report.write(f"[ข้าม] ไม่พบไฟล์ log (ค้นชื่อ {', '.join(LOG_CANDIDATES)}) — ระบุด้วย --log")
            report.write("[ผลขั้น 3] ⚠️ ยังไม่มีหลักฐานว่าต่อติด (หาไฟล์ log ไม่เจอ)")
            problems.append("ไม่พบไฟล์ log ของโปรแกรม — ระบุตำแหน่งด้วย --log")
        else:
            cli = [str(log_path), "--stale-minutes", "10"]
            if args.follow:
                cli += ["--follow", str(args.follow)]
            if config_path and config_path.is_file():
                cli += ["--config", str(config_path)]
            code, _text = report.capture(ccl.main, cli)
            if code == 0:
                report.write("[ผลขั้น 3] ✅ log มี pusher:connection_established")
            elif code == 5:
                report.write("[ผลขั้น 3] ⚠️ อ่าน log ไม่ได้ — ข้ามได้ถ้าโปรแกรมคุณเขียน log ไว้ที่อื่น")
            else:
                report.write(f"[ผลขั้น 3] ❌ ยังไม่ต่อติด (รหัส {code}) — ดูรายละเอียดด้านบน")
                problems.append(f"log ยังไม่ต่อติด (รหัส {code})")

    # ---------- 4) บริบทของเครื่อง ----------
    report.section("4) บริบทของเครื่อง (process / proxy / hosts)")
    for line in environment_context():
        report.write(line)

    # ---------- สรุป ----------
    report.section("สรุปท้ายรายงาน")
    if not problems:
        report.write("✅ ทุกขั้นผ่าน — ถ้าโปรแกรมยังไม่แจ้งเตือน ให้ตรวจฝั่งโปรแกรมนั้น (ไม่ได้อยู่ในรีโปนี้)")
    else:
        report.write("❌ ยังมีจุดที่ต้องแก้:")
        for item in problems:
            report.write(f"   - {item}")
        report.write("")
        report.write("ลำดับที่แนะนำ:")
        report.write("   1) ปิดทุก process:  taskkill /IM python.exe /F")
        report.write("   2) เปิดโปรแกรม 1 ตัว แล้วรันเครื่องมือนี้ซ้ำ")
        report.write("   3) ถ้ายังไม่ผ่าน ส่งไฟล์รายงานนี้กลับมาให้ดู")
    report.write("")
    report.write(f"(รายงานฉบับเต็มถูกเขียนไว้ที่ {out_path})")

    report.save(out_path)

    if args.quiet:
        print(f"── สรุป ── (รายละเอียดในไฟล์ {out_path})")
        for line in report.buffer.getvalue().splitlines()[-12:]:
            print(line)

    print()
    print(f"📄 ส่งไฟล์นี้กลับมาได้เลย: {out_path}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
