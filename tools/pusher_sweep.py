#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pusher_sweep.py — ยิงทดสอบ "ทุก cluster × ทุก key" ของ Pusher ที่ Kick ใช้ แล้วสรุปเป็นตารางเดียว

ทำไมต้องมี: error `App key ... not in this cluster` ต้องรู้ให้ได้ว่า "cluster ไหน + key ไหน"
ที่ Pusher ยอมรับจริง การไล่ทีละคู่ด้วย ws_probe.py จะช้าและต้องคัดลอกผลเอง สคริปต์นี้ทำทั้งหมด
พร้อมกัน แล้วบอกคำตอบเป็นบรรทัดเดียว (พร้อม URL ที่ใช้ได้จริง)

รัน:
    python tools\\pusher_sweep.py
    python tools\\pusher_sweep.py --keys 32cbd69e4b950bf97679,34bf7a0ff419a2a775b9 --timeout 5
    python tools\\pusher_sweep.py --json sweep_result.json
    python tools\\pusher_sweep.py --check-kick          # ตรวจด้วยว่าเน็ตนี้เข้า kick.com ได้ไหม

รหัสออก: 0 = เจอคู่ที่ใช้ได้ · 3 = ไม่มีคู่ไหนใช้ได้เลย · 2 = ใช้คำสั่งผิด

ใช้ไลบรารีมาตรฐานล้วน (ไม่ต้อง pip install) และใช้ตัวช่วยจาก ws_probe.py ในรีโปเดียวกัน
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
if str(REPO_DIR) not in sys.path:
    sys.path.insert(0, str(REPO_DIR))

import ws_probe as wp  # noqa: E402

# key ที่ Kick ใช้อยู่จริงตอนนี้ + key เก่าที่ถูกปิดแล้ว (ใส่ไว้เพื่อให้เห็นหลักฐานว่าเก่าจริง)
KEYS_DEFAULT = [wp.DEFAULT_KEY, *wp.RETIRED_KEYS]
# cluster ที่ Pusher เปิดให้ใช้จริงในภูมิภาคต่าง ๆ
CLUSTERS_DEFAULT = ["us2", "us3", "us1", "eu", "ap1", "ap2", "ap3", "ap4", "mt1", "sa1", "in1"]
VERSION = wp.PUSHER_VERSION

VERDICT_LABEL = {
    "ok": "✅ ต่อได้",
    "wrong": "❌ ผิด cluster/key",
    "error": "⚠️ Pusher error",
    "http": "❌ HTTP ไม่ใช่ 101",
    "timeout": "⌛ หมดเวลา",
    "tls": "❌ TLS ถูกตัด",
    "tcp": "❌ ต่อ TCP ไม่ได้",
    "dns": "❌ DNS ใช้ไม่ได้",
    "handshake": "❌ handshake ไม่มีคำตอบ",
}


def build_url(host: str, key: str, scheme: str = "wss", port: int | None = None) -> str:
    suffix = f":{port}" if port and port not in (80, 443) else ""
    return f"{scheme}://{host}{suffix}/app/{key}?protocol=7&client=js&version={VERSION}&flash=false"


def test_pair(host: str, cluster: str, key: str, timeout: float, scheme: str, port_override: int | None) -> dict:
    """ทดสอบคู่เดียว คืนผลแบบ dict (ไม่โยน exception ออกมา)"""
    port = port_override or (443 if scheme == "wss" else 80)
    url = build_url(host, key, scheme, port)
    row: dict = {
        "cluster": cluster, "key": key, "url": url, "verdict": "timeout",
        "detail": "", "key_seen": None, "socket_id": None, "elapsed_ms": None,
    }
    started = time.time()

    def finish(verdict: str, detail: str = "", **extra) -> dict:
        row["verdict"] = verdict
        row["detail"] = detail
        row.update(extra)
        row["elapsed_ms"] = int((time.time() - started) * 1000)
        for closing in (locals().get("tls_sock"), locals().get("sock")):
            try:
                if closing is not None:
                    closing.close()
            except OSError:
                pass
        return row

    parsed = urllib.parse.urlparse(url)
    try:
        ips = wp.resolve(parsed.hostname or host, port)
    except OSError as exc:
        return finish("dns", f"{type(exc).__name__}: {exc}")
    if not ips:
        return finish("dns", "แก้ชื่อโฮสต์ไม่ได้")

    sock = None
    tls_sock = None
    try:
        sock = socket.create_connection((parsed.hostname or host, port), timeout=timeout)
    except OSError as exc:
        return finish("tcp", f"{type(exc).__name__}: {exc}")

    if scheme == "wss":
        try:
            tls_sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname or host)
        except ssl.SSLError as exc:
            return finish("tls", f"{type(exc).__name__}: {exc}")
        active = tls_sock
    else:
        active = sock

    request = (
        f"GET {parsed.path}?{parsed.query} HTTP/1.1\r\n"
        f"Host: {parsed.hostname}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {wp.base64.b64encode(os.urandom(16)).decode()}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Origin: https://kick.com\r\n"
        "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) pusher_sweep.py\r\n\r\n"
    )
    try:
        active.sendall(request.encode())
        status, _headers, leftover = wp.read_http_headers(active, timeout)
    except (TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
        return finish("handshake", f"{type(exc).__name__}: {exc}")

    if "101" not in status:
        return finish("http", status.strip())

    reader = wp.WSReader(active, leftover)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            frame = reader.next_frame(max(0.5, deadline - time.time()))
        except (TimeoutError, ConnectionError, OSError, ssl.SSLError) as exc:
            return finish("timeout", f"{type(exc).__name__}: {exc}")
        if frame is None:
            continue
        try:
            data = json.loads(frame)
        except json.JSONDecodeError:
            continue
        event = data.get("event", "?")
        inner = data.get("data")
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except json.JSONDecodeError:
                pass
        message = str(inner.get("message") or "") if isinstance(inner, dict) else ""
        if event == "pusher:connection_established":
            socket_id = inner.get("socket_id") if isinstance(inner, dict) else None
            return finish("ok", "pusher:connection_established", socket_id=socket_id)
        if event == "pusher:error":
            match = re.search(r"App key\s+(\S+?)\s+(?:is\s+)?not in this cluster", message, re.I)
            if not match:
                match = re.search(r"App key\s+(\S+)", message, re.I)
            seen = match.group(1) if match else None
            if "not in this cluster" in message.lower():
                return finish("wrong", message, key_seen=seen)
            return finish("error", message or json.dumps(data, ensure_ascii=False)[:200], key_seen=seen)
    return finish("timeout", f"ไม่ได้ข้อความจาก Pusher ใน {timeout:.0f} วินาที")


def check_kick_reachability(timeout: float = 8.0) -> list[dict]:
    """ตรวจว่าเครือข่ายนี้เข้าหน้า Kick และ API ทางการได้ไหม (ช่วยแยกปัญหาเน็ต/ภูมิภาค)"""
    targets = [
        ("kick.com", "https://kick.com/"),
        ("api.kick.com", "https://api.kick.com/public/v1/public-key"),
    ]
    results = []
    for name, url in targets:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 pusher_sweep.py"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                results.append({"target": name, "status": resp.status, "cf_ray": resp.headers.get("CF-Ray"),
                                "server": resp.headers.get("Server")})
        except urllib.error.HTTPError as exc:
            results.append({"target": name, "status": exc.code, "cf_ray": exc.headers.get("CF-Ray"),
                            "server": exc.headers.get("Server")})
        except Exception as exc:  # noqa: BLE001 — รายงานทุกความล้มเหลวให้เห็น
            results.append({"target": name, "status": None, "error": f"{type(exc).__name__}: {exc}"})
    return results


def render_table(rows: list[dict]) -> str:
    order = {"ok": 0, "wrong": 1, "error": 2, "http": 3, "timeout": 4, "handshake": 5, "tls": 6, "tcp": 7, "dns": 8}
    rows = sorted(rows, key=lambda r: (order.get(r["verdict"], 9), r["cluster"], r["key"]))
    widths = (
        max([len("cluster")] + [len(r["cluster"]) for r in rows]),
        max([len("key")] + [len(r["key"]) for r in rows]),
    )
    lines = [
        f"{'cluster'.ljust(widths[0])}  {'key'.ljust(widths[1])}  ผลลัพธ์",
        "-" * (widths[0] + widths[1] + 34),
    ]
    for row in rows:
        label = VERDICT_LABEL.get(row["verdict"], row["verdict"])
        detail = row["detail"]
        if row["verdict"] == "ok" and row.get("socket_id"):
            detail = f"socket_id={row['socket_id']}"
        if row["verdict"] == "wrong" and row.get("key_seen"):
            detail = f"Pusher เห็น key {row['key_seen']}"
        if len(detail) > 78:
            detail = detail[:75] + "..."
        lines.append(f"{row['cluster'].ljust(widths[0])}  {row['key'].ljust(widths[1])}  {label:<18} {detail}")
    return "\n".join(lines)


def summarize(rows: list[dict]) -> dict:
    winners = [r for r in rows if r["verdict"] == "ok"]
    best = next((w for w in winners if w["cluster"] == "us2"), winners[0] if winners else None)
    wrong_keys = {r["key_seen"] for r in rows if r["verdict"] == "wrong" and r.get("key_seen")}
    return {"winners": winners, "best": best, "keys_pusher_saw": sorted(wrong_keys)}


def main(argv: list[str] | None = None) -> int:
    wp._fix_stdout()
    parser = argparse.ArgumentParser(
        prog="pusher_sweep.py",
        description="ยิงทดสอบทุก cluster × ทุก key ของ Pusher ที่ Kick ใช้ แล้วสรุปคำตอบ",
    )
    parser.add_argument("--keys", help=f"คั่นด้วยจุลภาค (ค่าเริ่มต้น: {','.join(KEYS_DEFAULT)})")
    parser.add_argument("--clusters", help=f"คั่นด้วยจุลภาค (ค่าเริ่มต้น {len(CLUSTERS_DEFAULT)} cluster)")
    parser.add_argument("--timeout", type=float, default=6.0, help="หมดเวลาต่อคู่ (วินาที, ค่าเริ่มต้น 6)")
    parser.add_argument("--workers", type=int, default=6, help="จำนวนที่ยิงพร้อมกัน (ค่าเริ่มต้น 6)")
    parser.add_argument("--json", dest="json_out", help="บันทึกผลทั้งหมดเป็นไฟล์ JSON")
    parser.add_argument("--check-kick", action="store_true", help="ตรวจด้วยว่าเน็ตนี้เข้า kick.com/api.kick.com ได้ไหม")
    parser.add_argument("--discover", action="store_true",
                        help="ถาม Kick ก่อนว่าใช้ app ไหน แล้วทดสอบ key นั้นด้วย")
    parser.add_argument("--host-template", default=None, help="ใช้กับเทสต์ออฟไลน์ (ค่าเริ่มต้น ws-{{cluster}}.pusher.com)")
    parser.add_argument("--scheme", choices=("wss", "ws"), default="wss", help="ใช้ ws สำหรับเซิร์ฟเวอร์จำลองในเครื่อง")
    parser.add_argument("--port", type=int, default=None, help="บังคับพอร์ต (ใช้กับเซิร์ฟเวอร์จำลอง)")
    args = parser.parse_args(argv)

    if args.discover:
        print("=== ถาม Kick: ตอนนี้ให้เชื่อมต่อ Pusher app ไหน ===")
        found, reason = wp.fetch_official_connection()
        if found:
            key, cluster = found
            print(f"[OK]  Kick บอก: key={key} · cluster={cluster}")
            if not args.keys:
                keys_extra = key
            if not args.clusters:
                args.clusters = cluster
        else:
            print(f"[!!]  ถามไม่ได้: {reason} — ใช้ค่าเริ่มต้นที่ฝังไว้แทน")
        print()

    keys = [k.strip() for k in (args.keys or ",".join(KEYS_DEFAULT)).split(",") if k.strip()]
    clusters = [c.strip() for c in (args.clusters or ",".join(CLUSTERS_DEFAULT)).split(",") if c.strip()]
    if not keys or not clusters:
        parser.error("ต้องมี key และ cluster อย่างน้อยอย่างละ 1 ค่า")

    print("=" * 78)
    print("pusher_sweep.py — หา cluster + key ที่ Pusher ยอมรับจริง")
    print("=" * 78)
    print(f"keys ({len(keys)}): {', '.join(keys)}")
    print(f"clusters ({len(clusters)}): {', '.join(clusters)}")
    print(f"โหมด: {args.scheme} · หมดเวลา {args.timeout:.0f}s/คู่ · ยิงพร้อมกัน {args.workers} คู่")
    print()

    host_template = args.host_template or wp.HOST_TEMPLATE
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {
            pool.submit(
                test_pair, host_template.format(cluster=cluster), cluster, key,
                args.timeout, args.scheme, args.port,
            ): (cluster, key)
            for cluster in clusters
            for key in keys
        }
        done = 0
        for future in futures:
            row = future.result()
            rows.append(row)
            done += 1
            symbol = "✅" if row["verdict"] == "ok" else ("❌" if row["verdict"] != "error" else "⚠️")
            print(f"  [{done}/{len(futures)}] {symbol} {row['cluster']:<4} {row['key']}  {VERDICT_LABEL.get(row['verdict'], '')}",
                  flush=True)

    print()
    print(render_table(rows))

    if args.check_kick:
        print()
        print("=== ตรวจการเข้าถึงของเครือข่ายนี้ ===")
        for item in check_kick_reachability():
            extra = item.get("error") or f"HTTP {item.get('status')}"
            if item.get("cf_ray"):
                extra += f" · CF-Ray {item['cf_ray']}"
            print(f"  {item['target']:<14} {extra}")

    summary = summarize(rows)
    print()
    if summary["best"]:
        best = summary["best"]
        print(">>> ✅ เจอคู่ที่ใช้ได้ — ใช้ค่านี้:")
        print(f"    {best['url']}")
        others = [w for w in summary["winners"] if w is not best]
        if others:
            print(f"    (ใช้ได้อีก {len(others)} คู่: " + ", ".join(f"{w['cluster']}/{w['key']}" for w in others) + ")")
        print()
        print("    ยืนยันซ้ำด้วย ws_probe.py:")
        print(f'      python ws_probe.py --url "{best["url"]}"')
    else:
        print(">>> ❌ ไม่มีคู่ไหนใช้ได้จากเครือข่ายนี้")
        if summary["keys_pusher_saw"]:
            print(f"    Pusher บอกว่ามันเห็น key: {', '.join(summary['keys_pusher_saw'])}")
            print("    ถ้า key ที่มันเห็น 'ไม่ตรง' กับที่คุณส่ง = โค้ดของคุณใช้ค่าอื่นตอนรัน")
        print("    ลำดับถัดไป: เปิด kick_probe.html ในเบราว์เซอร์เครื่องนี้แล้ววางผล Console (ข้อ 3 ของหน้านั้น)")
        print("    หรือใช้ Events API (webhook) แทน Pusher — ไม่ต้องพึ่ง key นี้เลย")

    if args.json_out:
        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "mode": args.scheme,
            "host_template": host_template,
            "timeout": args.timeout,
            "rows": rows,
            "winners": [w["url"] for w in summary["winners"]],
            "keys_pusher_saw": summary["keys_pusher_saw"],
        }
        Path(args.json_out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n(บันทึกผลทั้งหมดไว้ที่ {args.json_out})")

    # ถ้ารันบน GitHub Actions ให้แนบผลลงหน้า summary ของ run ด้วย
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        try:
            with open(step_summary, "a", encoding="utf-8") as handle:
                handle.write("## ผลการไล่หา cluster + key\n\n```\n")
                handle.write(render_table(rows) + "\n```\n")
                if summary["best"]:
                    handle.write(f"\n**ใช้ค่านี้:** `{summary['best']['url']}`\n")
                else:
                    handle.write("\n**ไม่พบคู่ที่ใช้ได้จากเครือข่ายนี้**\n")
        except OSError:
            pass

    return 0 if summary["winners"] else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
