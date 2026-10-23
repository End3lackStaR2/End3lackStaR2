#!/usr/bin/env python3
"""Loopback bridge between Friday (local scripts) and a browser tab.

Friday has no way to call Arena directly: Arena has no API for machines and only
reacts to messages typed by a human. This bridge implements the other direction -
a browser extension standing in for that human:

    Friday  --HTTP-->  bridge (127.0.0.1)  <--poll--  extension  --types-->  Arena tab
                             ^                                    |
                             +----------- answer ------------------+

So "Friday asks Arena" really means "Friday drives the tab you left open".
It is UI automation: it types as you, in your session. Read README-TH.md first.

Commands:
    serve     run the loopback server (the extension talks to this)
    ask       queue a question and wait for the answer (used by Friday)
    status    print queue / attachment state
    selftest  prove the protocol works without a browser

Security model
    * binds 127.0.0.1 only, never 0.0.0.0
    * every request needs the token from bus/.local/bridge.token
      (0666-free: created 0600 on unix; the token never leaves the machine)
    * one question in flight, min interval between questions, max queue length
    * answers are length-capped and logged to bus/.local/bridge.jsonl (audit)
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_TOKEN = REPO / "bus" / ".local" / "bridge.token"
AUDIT = REPO / "bus" / ".local" / "bridge.jsonl"

MAX_QUESTION_CHARS = 8000
MAX_ANSWER_CHARS = 120_000
MAX_QUEUE = 3


# --------------------------------------------------------------------------- state
class Bridge:
    def __init__(self, token: str, min_interval: float = 15.0, audit: Path | None = None):
        self.token = token
        self.min_interval = min_interval
        self.audit_path = audit
        self.lock = threading.Lock()
        self.inbox: deque = deque()
        self.answers: dict[str, dict] = {}
        self.events: dict[str, threading.Event] = {}
        self.in_flight: dict | None = None
        self.last_seen = 0.0
        self.last_ask = 0.0
        self.asked = 0
        self.answered = 0
        self.expired = 0

    # -- audit
    def audit(self, **rec):
        if not self.audit_path:
            return
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **rec},
                                    ensure_ascii=False) + "\n")
        except OSError:
            pass

    # -- producer side
    def ask(self, text: str, wait: float) -> dict:
        text = (text or "").strip()
        if not text:
            return {"ok": False, "reason": "empty question"}
        if len(text) > MAX_QUESTION_CHARS:
            return {"ok": False, "reason": f"question longer than {MAX_QUESTION_CHARS} chars"}

        with self.lock:
            now = time.time()
            if self.in_flight or self.inbox:
                return {"ok": False, "reason": "busy", "pending": len(self.inbox)}
            if now - self.last_ask < self.min_interval:
                return {"ok": False, "reason": "rate_limited",
                        "retry_in": round(self.min_interval - (now - self.last_ask), 1)}
            if len(self.inbox) >= MAX_QUEUE:
                return {"ok": False, "reason": "queue_full"}
            task_id = "A-" + time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
            task = {"id": task_id, "text": text, "created_at": round(now, 3)}
            self.inbox.append(task)
            self.in_flight = task
            ev = threading.Event()
            self.events[task_id] = ev
            self.last_ask = now
            self.asked += 1
        self.audit(event="ask", id=task_id, chars=len(text))

        finished = ev.wait(timeout=max(0.0, wait))
        with self.lock:
            answer = self.answers.get(task_id)
            error = (answer or {}).get("error")
            if not finished:
                self.expired += 1
                # let the extension drop it instead of typing a stale question later
                if self.in_flight and self.in_flight["id"] == task_id:
                    self.in_flight = None
                self.events.pop(task_id, None)
                if self.inbox and self.inbox[0]["id"] == task_id:
                    self.inbox.popleft()
        if error:
            self.audit(event="failed", id=task_id, reason=error)
            return {"ok": False, "reason": f"extension error: {error}", "id": task_id}
        if not finished:
            self.audit(event="timeout", id=task_id)
            return {"ok": False, "reason": "timeout", "id": task_id,
                    "hint": "extension not attached, or the tab was closed / logged out"}
        return {"ok": True, "id": task_id, "answer": answer["answer"],
                "partial": answer.get("partial", False), "seconds": answer.get("seconds")}

    # -- extension side
    def next_task(self) -> dict | None:
        with self.lock:
            self.last_seen = time.time()
            if not self.inbox:
                return None
            return dict(self.inbox[0])

    def submit(self, task_id: str, answer: str, partial: bool = False) -> dict:
        answer = (answer or "").strip()
        if not answer:
            return {"ok": False, "reason": "empty answer"}
        answer = answer[:MAX_ANSWER_CHARS]
        with self.lock:
            self.last_seen = time.time()
            if not self.in_flight or self.in_flight["id"] != task_id:
                return {"ok": False, "reason": "unknown or expired task"}
            self.answers[task_id] = {"answer": answer, "partial": partial, "seconds": 0}
            self.in_flight = None
            if self.inbox and self.inbox[0]["id"] == task_id:
                self.inbox.popleft()
            ev = self.events.pop(task_id, None)
            self.answered += 1
        if ev:
            ev.set()
        self.audit(event="answer", id=task_id, chars=len(answer), partial=partial)
        return {"ok": True}

    def fail(self, task_id: str, reason: str) -> dict:
        """The extension could not do it - release the caller immediately, no guesswork."""
        with self.lock:
            self.last_seen = time.time()
            if not self.in_flight or self.in_flight["id"] != task_id:
                return {"ok": False, "reason": "unknown or expired task"}
            self.answers[task_id] = {"error": reason or "unknown extension error"}
            self.in_flight = None
            if self.inbox and self.inbox[0]["id"] == task_id:
                self.inbox.popleft()
            ev = self.events.pop(task_id, None)
        if ev:
            ev.set()
        self.audit(event="fail", id=task_id, reason=reason)
        return {"ok": True}

    def health(self) -> dict:
        with self.lock:
            attached = (time.time() - self.last_seen) < 15 if self.last_seen else False
            return {"ok": True, "attached": attached,
                    "last_seen_seconds": round(time.time() - self.last_seen, 1) if self.last_seen else None,
                    "pending": len(self.inbox), "in_flight": self.in_flight["id"] if self.in_flight else None,
                    "asked": self.asked, "answered": self.answered, "expired": self.expired}


# --------------------------------------------------------------------------- http
def make_handler(bridge: Bridge):
    class Handler(BaseHTTPRequestHandler):
        server_version = "FridayBridge/1.0"

        def log_message(self, fmt, *args):  # keep the console clean
            if os.environ.get("FRIDAY_BRIDGE_DEBUG"):
                sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        # -- helpers
        def _send(self, code: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorised(self) -> bool:
            got = self.headers.get("X-Bridge-Token", "")
            return bool(got) and hmac.compare_digest(got, bridge.token)

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 2_000_000:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        # -- routes
        def do_GET(self):
            if not self._authorised():
                return self._send(401, {"ok": False, "reason": "bad token"})
            if self.path.split("?")[0] == "/health":
                return self._send(200, bridge.health())
            return self._send(404, {"ok": False, "reason": "not found"})

        def do_POST(self):
            if not self._authorised():
                return self._send(401, {"ok": False, "reason": "bad token"})
            route = self.path.split("?")[0]
            body = self._body()
            if route == "/next":
                task = bridge.next_task()
                return self._send(200, {"ok": True, "task": task})
            if route == "/answer":
                return self._send(200, bridge.submit(body.get("id", ""), body.get("text", ""),
                                                    bool(body.get("partial"))))
            if route == "/fail":
                return self._send(200, bridge.fail(body.get("id", ""), body.get("reason", "")))
            if route == "/ask":
                wait = body.get("wait", 300)
                try:
                    wait = max(0.0, min(float(wait), 1800.0))
                except (TypeError, ValueError):
                    wait = 300.0
                return self._send(200, bridge.ask(body.get("text", ""), wait))
            if route == "/cancel":
                with bridge.lock:
                    bridge.in_flight = None
                    bridge.inbox.clear()
                return self._send(200, {"ok": True})
            return self._send(404, {"ok": False, "reason": "not found"})

    return Handler


def ensure_token(path: Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 16:
            return token
    token = secrets.token_hex(24)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token


# --------------------------------------------------------------------------- client
def client_post(port: int, token: str, route: str, payload: dict, timeout: float = 40):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Bridge-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "reason": f"http {exc.code}"}
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return {"ok": False, "reason": f"bridge not reachable: {exc}"}


def client_get(port: int, token: str, route: str, timeout: float = 5):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{port}{route}",
                                 headers={"X-Bridge-Token": token}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "reason": f"http {exc.code}"}
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return {"ok": False, "reason": f"bridge not reachable: {exc}"}


# --------------------------------------------------------------------------- selftest
def selftest(verbose: bool = True) -> int:
    passed = failed = 0

    def check(name, cond, extra=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            if verbose:
                print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name} {extra}")

    token = "t" * 32
    bridge = Bridge(token, min_interval=0.0, audit=None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(bridge))
    httpd.daemon_threads = True
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        bad = client_get(port, "wrong", "/health")
        check("rejects a wrong token", bad.get("reason") == "bad token", str(bad))

        health = client_get(port, token, "/health")
        check("health responds", health.get("ok") is True, str(health))
        check("nothing attached yet", health.get("attached") is False, str(health))

        result = {}

        def asking():
            result.update(client_post(port, token, "/ask", {"text": "what now?", "wait": 5}, timeout=10))

        th = threading.Thread(target=asking)
        th.start()
        time.sleep(0.3)

        nxt = client_post(port, token, "/next", {})
        check("extension sees the question", (nxt.get("task") or {}).get("text") == "what now?", str(nxt))
        task_id = nxt["task"]["id"]
        check("health shows attached after a poll", client_get(port, token, "/health").get("attached") is True)

        busy = client_post(port, token, "/ask", {"text": "second", "wait": 0}, timeout=5)
        check("refuses a second question while one is in flight", busy.get("reason") == "busy", str(busy))

        ack = client_post(port, token, "/answer", {"id": task_id, "text": "do X then Y"})
        check("answer accepted", ack.get("ok") is True, str(ack))
        th.join(10)
        check("ask returns the answer", result.get("ok") is True and result.get("answer") == "do X then Y",
              str(result))

        stale = client_post(port, token, "/answer", {"id": task_id, "text": "late"})
        check("a late answer is refused", stale.get("ok") is False, str(stale))

        timed = client_post(port, token, "/ask", {"text": "nobody answers", "wait": 0.4}, timeout=10)
        check("times out instead of hanging", timed.get("reason") == "timeout", str(timed))
        check("the queue is drained after a timeout", client_post(port, token, "/next", {}).get("task") is None)
    finally:
        httpd.shutdown()

    # token file handling
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "sub" / "bridge.token"
        t1 = ensure_token(p)
        t2 = ensure_token(p)
        check("token file is created once and reused", t1 == t2 and len(t1) >= 32)

    print(f"\nself test: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


# --------------------------------------------------------------------------- cli
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Loopback bridge between Friday and an Arena browser tab.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--token-file", default=str(DEFAULT_TOKEN))
    s.add_argument("--min-interval", type=float, default=15.0)
    s.add_argument("--audit", default=str(AUDIT))

    a = sub.add_parser("ask")
    a.add_argument("text")
    a.add_argument("--wait", type=float, default=300.0)
    a.add_argument("--port", type=int, default=8787)
    a.add_argument("--token-file", default=str(DEFAULT_TOKEN))
    a.add_argument("--json", action="store_true")

    st = sub.add_parser("status")
    st.add_argument("--port", type=int, default=8787)
    st.add_argument("--token-file", default=str(DEFAULT_TOKEN))

    se = sub.add_parser("selftest")

    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()

    if args.cmd == "serve":
        token = ensure_token(Path(args.token_file))
        audit = Path(args.audit) if args.audit.lower() != "none" else None
        bridge = Bridge(token, min_interval=args.min_interval, audit=audit)
        httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(bridge))
        httpd.daemon_threads = True
        print(f"bridge listening on http://127.0.0.1:{args.port}")
        print(f"token file: {args.token_file}")
        print("load the extension, click its icon on the Arena tab, paste the token, press Attach.")
        print("Ctrl+C to stop.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped")
        finally:
            httpd.server_close()
        return 0

    if args.cmd == "status":
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
        out = client_get(args.port, token, "/health")
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0 if out.get("ok") else 1

    if args.cmd == "ask":
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
        out = client_post(args.port, token, "/ask", {"text": args.text, "wait": args.wait},
                          timeout=args.wait + 30)
        if args.json:
            print(json.dumps(out, ensure_ascii=False))
        elif out.get("ok"):
            print(out["answer"])
        else:
            print(f"[bridge] {out.get('reason')}" + (f" ({out.get('hint')})" if out.get("hint") else ""),
                  file=sys.stderr)
        return 0 if out.get("ok") else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
