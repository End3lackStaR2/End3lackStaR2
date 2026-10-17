#!/usr/bin/env python3
"""The test page used to verify the extension must itself work.

Run: python -m unittest -v test_mock_chat      (from bridge/tests)
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOCK = HERE.parent / "mock_chat.py"
PAGE = HERE.parent / "mock_chat.html"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockChatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = free_port()
        cls.proc = subprocess.Popen([sys.executable, str(MOCK), "--port", str(cls.port)],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{cls.port}/", timeout=2) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        raise RuntimeError("mock chat did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        if cls.proc.stdout:
            cls.proc.stdout.close()

    def test_page_has_everything_content_js_needs(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=5) as resp:
            body = resp.read().decode("utf-8")
        self.assertIn('<textarea id="composer"', body, "a composer is required")
        self.assertIn("type=\"submit\"", body, "a send button is required")
        self.assertIn('id="transcript"', body, "a scroll container is required")
        # the source holds a regex literal, so the brackets are escaped there
        self.assertIn(r"friday:[a-z0-9-]+", body, "the mock must recognise markers with hyphens")
        self.assertIn("/i", body)

    def test_the_page_actually_replies(self):
        """The reply logic lives in page JS, so assert its shape in the source."""
        body = PAGE.read_text(encoding="utf-8")
        self.assertIn("setTimeout", body)
        self.assertIn("bubble(reply", body)
        self.assertIn("คำตอบจำลอง", body)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/nope", timeout=5)
        self.assertEqual(ctx.exception.code, 404)

    def test_marker_regex_matches_what_content_js_emits(self):
        body = PAGE.read_text(encoding="utf-8")
        pattern = re.search(r"text\.match\(/(.+?)/i\)", body)
        self.assertIsNotNone(pattern, "the mock should extract the marker with a regex")
        emitted = "[friday:" + "a-1" + "]"
        self.assertIsNotNone(re.search(pattern.group(1), emitted, re.I))


if __name__ == "__main__":
    unittest.main(verbosity=2)
