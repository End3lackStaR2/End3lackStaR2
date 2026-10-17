#!/usr/bin/env python3
"""End-to-end tests for the loopback bridge, over real HTTP.

A fake "extension" plays the browser side, so the whole protocol is exercised
without Chrome: auth, queueing, one-in-flight, answer delivery, extension-side
failure, timeout, and the audit trail.

Run: python -m unittest -v test_bridge_e2e      (from bridge/tests)
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parent / "arena_bridge.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http(port, token, route, payload=None, timeout=15):
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{port}{route}"
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", "X-Bridge-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


class BridgeE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = free_port()
        cls.token_file = Path(cls.tmp.name) / "bridge.token"
        cls.audit = Path(cls.tmp.name) / "bridge.jsonl"
        cls.proc = subprocess.Popen(
            [sys.executable, str(BRIDGE), "serve", "--port", str(cls.port),
             "--token-file", str(cls.token_file), "--audit", str(cls.audit), "--min-interval", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        cls.token = ""
        deadline = time.time() + 15
        while time.time() < deadline:
            if cls.token_file.exists():
                cls.token = cls.token_file.read_text(encoding="utf-8").strip()
                if cls.token:
                    break
            time.sleep(0.1)
        # wait until it actually answers
        while time.time() < deadline:
            try:
                code, body = http(cls.port, cls.token, "/health", timeout=2)
                if code == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("bridge did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        cls.tmp.cleanup()

    def health(self):
        return http(self.port, self.token, "/health")[1]

    def drain(self):
        while True:
            task = http(self.port, self.token, "/next", {})[1].get("task")
            if not task:
                return
            http(self.port, self.token, "/answer", {"id": task["id"], "text": "cleared"})

    def test_01_wrong_token_is_refused(self):
        code, body = http(self.port, "nope", "/health")
        self.assertEqual(code, 401)
        self.assertEqual(body["reason"], "bad token")

    def test_02_question_and_answer_round_trip(self):
        result = {}

        def ask():
            result.update(http(self.port, self.token, "/ask", {"text": "hello arena", "wait": 10})[1])

        th = threading.Thread(target=ask)
        th.start()

        deadline = time.time() + 5
        task = None
        while time.time() < deadline and not task:
            task = http(self.port, self.token, "/next", {})[1].get("task")
            if not task:
                time.sleep(0.05)
        self.assertIsNotNone(task, "the extension should see the question")
        self.assertEqual(task["text"], "hello arena")
        self.assertTrue(self.health()["attached"])

        ack = http(self.port, self.token, "/answer", {"id": task["id"], "text": "reply!"})[1]
        self.assertTrue(ack["ok"])
        th.join(10)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result["answer"], "reply!")

        self.assertFalse(http(self.port, self.token, "/answer", {"id": task["id"], "text": "late"})[1]["ok"])

    def test_03_second_question_while_busy_is_refused(self):
        result = {}
        th = threading.Thread(target=lambda: result.update(
            http(self.port, self.token, "/ask", {"text": "hold", "wait": 2})[1]))
        th.start()
        time.sleep(0.3)
        busy = http(self.port, self.token, "/ask", {"text": "second", "wait": 0})[1]
        self.assertEqual(busy.get("reason"), "busy", busy)
        self.drain()
        th.join(10)

    def test_04_extension_failure_returns_immediately(self):
        result = {}

        def ask():
            result.update(http(self.port, self.token, "/ask", {"text": "will fail", "wait": 20})[1])

        th = threading.Thread(target=ask)
        th.start()
        task = None
        deadline = time.time() + 5
        while time.time() < deadline and not task:
            task = http(self.port, self.token, "/next", {})[1].get("task")
            time.sleep(0.05)
        self.assertIsNotNone(task)

        started = time.time()
        http(self.port, self.token, "/fail", {"id": task["id"], "reason": "no message box found"})
        th.join(10)
        self.assertLess(time.time() - started, 8, "a failure must not wait for the timeout")
        self.assertFalse(result.get("ok"))
        self.assertIn("no message box found", result.get("reason", ""))

    def test_05_timeout_drains_the_queue(self):
        out = http(self.port, self.token, "/ask", {"text": "nobody home", "wait": 0.5})[1]
        self.assertEqual(out.get("reason"), "timeout", out)
        self.assertIsNone(http(self.port, self.token, "/next", {})[1].get("task"))

    def test_06_cancel_clears_everything(self):
        th = threading.Thread(target=lambda: http(self.port, self.token, "/ask",
                                                  {"text": "stuck", "wait": 2}))
        th.start()
        time.sleep(0.3)
        self.assertTrue(http(self.port, self.token, "/cancel", {})[1]["ok"])
        th.join(10)
        self.assertIsNone(http(self.port, self.token, "/next", {})[1].get("task"))

    def test_07_audit_trail_exists(self):
        self.drain()
        http(self.port, self.token, "/ask", {"text": "audit me", "wait": 0.2})
        lines = [json.loads(x) for x in self.audit.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertTrue(any(x.get("event") == "ask" for x in lines))
        self.assertTrue(any("chars" in x for x in lines))
        joined = json.dumps(lines)
        self.assertNotIn("audit me", joined, "the audit stores sizes, not the text")

    def test_09_cli_ask_is_the_command_from_the_readme(self):
        """The exact command a user types, end to end, with a fake extension."""
        import threading
        import time as _time

        answer_text = "ตอบจาก CLI: ตรวจ log ก่อน"

        def fake_browser():
            deadline = _time.time() + 20
            while _time.time() < deadline:
                task = http(self.port, self.token, "/next", {})[1].get("task")
                if task:
                    http(self.port, self.token, "/answer", {"id": task["id"], "text": answer_text})
                    return
                _time.sleep(0.1)

        th = threading.Thread(target=fake_browser, daemon=True)
        th.start()
        _time.sleep(0.4)

        proc = subprocess.run(
            [sys.executable, str(BRIDGE), "ask", "hello from the command line",
             "--wait", "15", "--port", str(self.port), "--token-file", str(self.token_file)],
            capture_output=True, text=True, timeout=60)
        th.join(5)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.strip(), answer_text)

    def test_10_cli_ask_fails_cleanly_when_nobody_answers(self):
        proc = subprocess.run(
            [sys.executable, str(BRIDGE), "ask", "nobody will answer",
             "--wait", "0.6", "--port", str(self.port), "--token-file", str(self.token_file)],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("timeout", proc.stderr)

    def test_08_empty_question_is_rejected(self):
        out = http(self.port, self.token, "/ask", {"text": "   ", "wait": 0})[1]
        self.assertFalse(out.get("ok"))
        self.assertEqual(out.get("reason"), "empty question")


if __name__ == "__main__":
    unittest.main(verbosity=2)
