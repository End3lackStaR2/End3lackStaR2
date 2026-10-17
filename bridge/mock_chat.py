#!/usr/bin/env python3
"""A fake chat page in your own browser, to prove the extension works
before you point it at your real Arena tab.

    python bridge/mock_chat.py            -> http://127.0.0.1:8799
    python bridge/mock_chat.py --port 9000

Then, in Chrome: load the extension, open this page, click the extension icon,
paste the token, press Attach, and run

    python bridge/arena_bridge.py ask "hello"

If the answer comes back, the bridge + extension + content script all work.
Whatever goes wrong after that is Arena's page, not your setup.
"""

from __future__ import annotations

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAGE = Path(__file__).with_name("mock_chat.html")


def make_handler():
    html = PAGE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "MockChat/1.0"

        def log_message(self, fmt, *args):
            sys.stderr.write("[mock] " + (fmt % args) + "\n")

        def do_GET(self):
            if self.path.split("?")[0] in ("/", "/index.html", "/chat"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(html)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html)
                return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Serve a local fake chat page for bridge testing.")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)

    if not PAGE.exists():
        print(f"missing {PAGE}", file=sys.stderr)
        return 2

    httpd = ThreadingHTTPServer((args.host, args.port), make_handler())
    print(f"mock chat: http://{args.host}:{args.port}")
    print("open it, attach the extension to that tab, then: python bridge/arena_bridge.py ask \"hello\"")
    print("Ctrl+C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
