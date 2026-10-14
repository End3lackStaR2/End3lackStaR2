#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_connection_log.py — อ่าน log ของโปรแกรมแจ้งเตือนของคุณ แล้วบอกทันทีว่า "ต่อติดหรือยัง"

ใช้เมื่อเพิ่งแก้ key ใน config.json เป็น `34bf7a0ff419a2a775b9` แล้วอยากรู้ว่าตัวที่รันอยู่
ได้ `pusher:connection_established` จริงไหม (หรือยังติด `4001 not in this cluster`)

รัน:
    python tools\\check_connection_log.py "D:\\HermesAI\\OBS\\files\\kick_notifier.log"
    python tools\\check_connection_log.py "D:\\...\\kick_notifier.log" --follow 60
    python tools\\check_connection_log.py "D:\\...\\kick_notifier.log" --config "D:\\...\\config.json"
    python tools\\check_connection_log.py "D:\\...\\kick_notifier.log" --expect-key 34bf7a0ff419a2a775b9

สิ่งที่มันตรวจ:
  1) ไฟล์ log ถูกเขียนอยู่จริงไหม (ถ้าไม่ → process ไม่ได้รัน หรือ log ไม่ได้ผูกกับไฟล์นี้)
  2) มี `pusher:connection_established` ไหม (พร้อม socket_id)
  3) มี `4001 / not in this cluster` ไหม และ Pusher บอกว่าเห็น key อะไร (เทียบกับ key ที่ควรเป็น)
  4) เจอ key ที่ Kick ปิดไปแล้ว (32cbd…) ใน log ไหม → แปลว่าตัวที่รันอยู่ยังใช้ค่าเก่า
  5) ปัญหาชั้นเน็ต: DNS / TCP / TLS (เช่น TLS ถูกตัด = มี proxy/security software)
  6) เทียบกับไฟล์ config ที่โปรแกรมอ่าน (ถ้าส่ง --config มาให้)

รหัสออก: 0 = ต่อติด · 3 = ถูกปฏิเสธ (4001) · 4 = ไม่พบหลักฐานใน log (หมดเวลารอ) · 5 = ไฟล์ log ไม่มี/อ่านไม่ได้ · 2 = ใช้คำสั่งผิด
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

import ws_probe as wp  # noqa: E402

DEFAULT_LOG_NAMES = ("kick_notifier.log", "notifier.log", "kick.log", "chat.log")

PATTERNS = {
    "established": re.compile(r"pusher:connection_established", re.I),
    "refused": re.compile(r"not in this cluster|[\"']?code[\"']?\s*[:=]\s*4001|\b4001\b", re.I),
    "handshake_ok": re.compile(r"HTTP/1\.1\s+101", re.I),
    "dns": re.compile(r"(getaddrinfo|nodename nor servname|name or service not known|DNS)", re.I),
    "tcp": re.compile(r"(ConnectionRefusedError|WinError 10061|ECONNREFUSED|timed out|ConnectionResetError)", re.I),
    "tls": re.compile(r"(SSLError|CERTIFICATE_VERIFY_FAILED|SSLZeroReturnError|EOF occurred|TLS)", re.I),
    "key_used": re.compile(r"App key\s+(\S+?)\s+(?:is\s+)?not in this cluster", re.I),
    "socket_id": re.compile(r'"socket_id"\s*:\s*"([^"]+)"'),
}


def find_default_log(start: Path | None = None) -> Path | None:
    """หาไฟล์ log ที่มีชื่อคุ้นเคยในโฟลเดอร์ที่กำหนด (หรือโฟลเดอร์ปัจจุบัน)"""
    base = start or Path.cwd()
    for name in DEFAULT_LOG_NAMES:
        candidate = base / name
        if candidate.is_file():
            return candidate
    # เผื่อชื่ออื่นที่มีคำว่า notifier/kick
    for candidate in sorted(base.glob("*.log")):
        if "notifier" in candidate.name.lower() or "kick" in candidate.name.lower():
            return candidate
    return None


def analyze(text: str, expect_key: str = "") -> dict:
    """วิเคราะห์ข้อความใน log แล้วคืนผลเป็น dict (ใช้ซ้ำได้ทั้งจาก CLI และเทสต์)"""
    result: dict = {
        "established": False, "refused": False, "handshake_ok": False,
        "socket_id": None, "keys_seen": [], "retired_in_log": [], "transport": [],
        "lines": [],
    }
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line:
            continue
        hit = False
        if PATTERNS["established"].search(line):
            result["established"] = True
            hit = True
            socket = PATTERNS["socket_id"].search(line)
            if socket:
                result["socket_id"] = socket.group(1)
        if PATTERNS["handshake_ok"].search(line):
            result["handshake_ok"] = True
            hit = True
        if PATTERNS["refused"].search(line):
            result["refused"] = True
            hit = True
            key_hit = PATTERNS["key_used"].search(line)
            if key_hit and key_hit.group(1) not in result["keys_seen"]:
                result["keys_seen"].append(key_hit.group(1))
        for label, pattern in (("DNS", PATTERNS["dns"]), ("TCP", PATTERNS["tcp"]), ("TLS", PATTERNS["tls"])):
            if pattern.search(line):
                result["transport"].append((label, line.strip()[:160]))
                hit = True
        for retired in wp.RETIRED_KEYS:
            if retired in line and retired not in result["retired_in_log"]:
                result["retired_in_log"].append(retired)
                hit = True
        if expect_key and expect_key in line:
            hit = True
        if hit:
            result["lines"].append(line.strip()[:200])
    return result


def decode_bytes(raw: bytes) -> str:
    """ถอดรหัสไฟล์ log ให้ทนทุกแบบที่เจอบน Windows

    ลำดับ: BOM UTF-16/UTF-8 -> เดา UTF-16 (มี NUL ถี่ ๆ) -> UTF-8 -> cp1252
    ไฟล์ log ที่โปรแกรมเขียนด้วย open() เปล่า ๆ บน Windows มักเป็น cp1252 ไม่ใช่ UTF-8
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", "replace")
    sample = raw[:400]
    if sample.count(b"\x00") > len(sample) // 4:
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def read_tail(path: Path, max_bytes: int = 400_000) -> str:
    """อ่านท้ายไฟล์ (ไม่โหลดทั้งไฟล์ถ้าใหญ่) แล้วถอดรหัสให้อัตโนมัติ"""
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()  # ตัดบรรทัดแรกที่อาจไม่ครบ
        return decode_bytes(handle.read())


def follow(path: Path, seconds: float, poll: float = 0.5):
    """นับว่าไฟล์ถูกเขียนเพิ่มไหม (generator แบบง่าย: คืนจำนวนบรรทัดใหม่รวม)"""
    start_size = path.stat().st_size if path.exists() else 0
    start_lines = len(read_tail(path).splitlines()) if path.exists() else 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(poll)
        if not path.exists():
            continue
        if path.stat().st_size != start_size:
            yield read_tail(path)
            start_size = path.stat().st_size
            start_lines = len(read_tail(path).splitlines())
    return


def verdict_for(info: dict, expect_key: str, stale_minutes: float | None) -> tuple[str, str, int]:
    """คืน (สถานะสั้น, คำอธิบาย, รหัสออก)"""
    if info["established"] and not info["refused"]:
        return "connected", "log มี pusher:connection_established — ต่อติดแล้ว", 0
    if info["retired_in_log"] and not info["refused"]:
        keys = ", ".join(info["retired_in_log"])
        return ("old_key", f"log ยังแสดง key เก่าที่ Kick ปิดแล้ว ({keys}) — "
                           "ตัวที่รันอยู่ใช้ค่าเก่า ให้ปิดแล้วเริ่มใหม่ 1 ตัว (และแก้ทุกไฟล์ที่ยังมี key นี้)", 3)
    if info["refused"]:
        seen = ", ".join(info["keys_seen"]) or "(อ่าน key ไม่ได้)"
        if any(key.lower() in wp.RETIRED_KEYS for key in info["keys_seen"]):
            return ("refused", f"ยังถูกปฏิเสธด้วย key เก่าที่ Kick ปิดแล้ว ({seen}) — "
                               "ตัวที่รันอยู่ยังอ่านค่าเก่า: ปิดทุก process เก่าแล้วสตาร์ทใหม่ 1 ตัว", 3)
        if expect_key and info["keys_seen"] and all(key.lower() != expect_key.lower() for key in info["keys_seen"]):
            return ("refused", f"ถูกปฏิเสธด้วย key {seen} (ไม่ใช่ {expect_key}) — "
                               "มีอะไรบางอย่าง override ค่า key อยู่ (env/ไฟล์อื่น/ค่า hardcode)", 3)
        return "refused", f"log ยังมี 4001 not in this cluster (key ที่ Pusher เห็น: {seen})", 3
    if stale_minutes is not None and stale_minutes >= 0:
        return ("stale", f"ไม่พบร่องรอยการเชื่อมต่อ และไฟล์นี้ไม่ได้ถูกเขียนมา {stale_minutes:.0f} นาที — "
                         "process ที่รันอยู่ไม่ได้เขียนไฟล์นี้ (หรือไม่ได้รัน)", 4)
    if info["transport"]:
        kinds = ", ".join(sorted({label for label, _ in info["transport"]}))
        return "transport", f"เจอปัญหาชั้นเน็ตใน log: {kinds} (ดูบรรทัดท้ายสุด)", 4
    return "unknown", "ไม่พบทั้ง connection_established และ 4001 — log อาจคนละไฟล์/ยังไม่ถึงเวลาต่อ", 4


def check_config_key(path: Path) -> tuple[str, str]:
    """ดึง key/cluster จากไฟล์ config ของโปรแกรม (ใช้ตัวแกะเดียวกับ ws_probe)"""
    try:
        key, url = wp.extract_from_config(str(path))
    except Exception as exc:  # noqa: BLE001
        return "", f"อ่าน config ไม่ได้: {type(exc).__name__}: {exc}"
    cluster = ""
    match = re.search(r"ws-([a-z0-9]+)\.pusher\.com", url or "")
    if match:
        cluster = match.group(1)
    if not cluster:
        # config บางแบบเก็บ cluster แยกเป็นฟิลด์ ไม่ได้ฝังใน wsUrl
        try:
            raw = path.read_text(encoding="utf-8-sig")
        except OSError:
            raw = ""
        field = re.search(r'["\']cluster["\']\s*[:=]\s*["\']([a-z0-9-]{1,32})["\']', raw, re.I)
        if field:
            cluster = field.group(1)
    if not key:
        return "", "ไม่พบ app key ในไฟล์ config นี้"
    note = "key ปัจจุบัน ✅" if key.lower() == wp.DEFAULT_KEY.lower() else (
        "key เก่าที่ Kick ปิดแล้ว ❌" if key.lower() in wp.RETIRED_KEYS else "key ที่ไม่รู้จัก ⚠️")
    return key, f"key={key} · cluster={cluster or '(ไม่ระบุ)'} · {note}"


def main(argv: list[str] | None = None) -> int:
    wp._fix_stdout()
    parser = argparse.ArgumentParser(
        prog="check_connection_log.py",
        description="ตรวจ log ของโปรแกรมแจ้งเตือนว่า Pusher ต่อติดหรือยัง",
    )
    parser.add_argument("log", nargs="?", help="path ของไฟล์ log (ไม่ใส่ = ค้นหาในโฟลเดอร์ปัจจุบัน)")
    parser.add_argument("--follow", type=float, default=0, metavar="วินาที",
                        help="เฝ้าดูต่ออีกกี่วินาที (ค่าเริ่มต้น 0 = อ่านเลย)")
    parser.add_argument("--config", help="ไฟล์ config ที่โปรแกรมอ่าน (จะตรวจ key ให้ด้วย)")
    parser.add_argument("--expect-key", dest="expect_key", default=wp.DEFAULT_KEY,
                        help=f"key ที่ควรถูกใช้ (ค่าเริ่มต้น {wp.DEFAULT_KEY})")
    parser.add_argument("--stale-minutes", dest="stale_minutes", type=float, default=10.0,
                        help="ถ้าไฟล์ log ไม่ถูกเขียนมานานกว่านี้ ให้เตือนว่า process ไม่ได้ทำงาน (0 = ปิดการเตือน)")
    args = parser.parse_args(argv)

    path = Path(args.log).expanduser() if args.log else find_default_log()
    if path is None or not path.is_file():
        print(f"[!!] ไม่พบไฟล์ log: {args.log or '(ค้นหาใน ' + str(Path.cwd()) + ')'}")
        print("     ตรวจว่า process ที่รันอยู่เขียน log ลงไฟล์อะไร (หรือส่ง path มาให้ตรง)")
        print(f"     ชื่อที่ค้นหาให้: {', '.join(DEFAULT_LOG_NAMES)}")
        return 5

    print("=" * 74)
    print("check_connection_log.py — ต่อติดหรือยัง?")
    print("=" * 74)
    size = path.stat().st_size
    age_minutes = (time.time() - path.stat().st_mtime) / 60
    print(f"ไฟล์ log : {path}")
    print(f"ขนาด     : {size:,} ไบต์ · แก้ไขล่าสุด {age_minutes:.1f} นาทีที่แล้ว")
    if args.config:
        cfg_path = Path(args.config).expanduser()
        if cfg_path.is_file():
            key, note = check_config_key(cfg_path)
            print(f"ไฟล์ config: {cfg_path}")
            print(f"           : {note}")
            if key and key.lower() in wp.RETIRED_KEYS:
                print("           ⚠️  config นี้ยังเป็น key เก่า — โปรแกรมที่รันจะยังถูกปฏิเสธแน่นอน")
        else:
            print(f"ไฟล์ config: {cfg_path} (ไม่พบ)")
    print()

    text = read_tail(path)
    info = analyze(text, expect_key=args.expect_key)

    if args.follow > 0:
        print(f"--- เฝ้าดูข้อความใหม่ {args.follow:.0f} วินาที (ให้เวลาโปรแกรมต่อใหม่) ---")
        for updated in follow(path, args.follow):
            info = analyze(updated, expect_key=args.expect_key)
            if info["established"] or info["refused"]:
                break
        print()

    stale = age_minutes if (args.stale_minutes > 0 and age_minutes > args.stale_minutes) else None
    status, detail, code = verdict_for(info, args.expect_key, stale)

    marks = {
        "connected": "✅", "refused": "❌", "old_key": "❌", "stale": "⚠️ ",
        "transport": "⚠️ ", "unknown": "❓",
    }
    print(f"ผลตรวจหลัก:")
    print(f"  {marks.get(status, '•')} {detail}")
    print()
    print("รายละเอียดที่พบ:")
    print(f"  pusher:connection_established : {'พบ' if info['established'] else 'ไม่พบ'}"
          + (f" (socket_id={info['socket_id']})" if info["socket_id"] else ""))
    print(f"  HTTP 101 (handshake ผ่าน)      : {'พบ' if info['handshake_ok'] else 'ไม่พบ'}")
    print(f"  4001 not in this cluster      : {'พบ' if info['refused'] else 'ไม่พบ'}"
          + (f" · key ที่ Pusher เห็น: {', '.join(info['keys_seen'])}" if info["keys_seen"] else ""))
    if info["retired_in_log"]:
        print(f"  key ที่ Kick ปิดแล้วใน log     : {', '.join(info['retired_in_log'])}")
    if info["transport"]:
        labels = ", ".join(sorted({label for label, _ in info["transport"]}))
        print(f"  ข้อผิดพลาดชั้นเน็ต             : {labels}")
    if info["lines"]:
        print()
        print("บรรทัดที่เกี่ยวข้อง (ท้ายสุด 5 บรรทัด):")
        for line in info["lines"][-5:]:
            print(f"  | {line}")
    else:
        print()
        print("(ไม่พบบรรทัดที่เกี่ยวข้องเลย — ตรวจว่าไฟล์นี้คือ log ของตัวที่รันอยู่จริง)")

    print()
    if status == "connected":
        print("ขั้นต่อไป: เปิด OBS/ไฟล์เสียงทดสอบ แล้วลองให้มีคนแชท หรือพิมพ์ทดสอบ")
    elif status in ("refused", "old_key"):
        print("ขั้นต่อไป:")
        print("  1) ปิดทุก process เก่า:  tasklist | findstr /i python   แล้ว  taskkill /IM python.exe /F")
        print("  2) ยืนยันค่าใหม่ด้วย:    python ws_probe.py --discover")
        print("  3) เปิด 1 process เท่านั้น แล้วรันเครื่องมือนี้ซ้ำด้วย --follow 60")
    elif status in ("stale", "unknown"):
        print("ขั้นต่อไป: เปิดโปรแกรมใหม่ (1 ตัว) แล้วรันเครื่องมือนี้ทันทีด้วย --follow 60")
        print("         ถ้าที่นั่นก็ยังไม่เขียน log เลย = โปรแกรมไม่ได้เขียน log ไฟล์นี้ หรือยังไม่ได้รัน")
    elif status == "transport":
        print("ขั้นต่อไป: ดูบรรทัดข้อผิดพลาดด้านบน (TLS/DNS/TCP) — ถ้าเป็น TLS ให้ตรวจ proxy/security software")

    return code


if __name__ == "__main__":
    raise SystemExit(main())
