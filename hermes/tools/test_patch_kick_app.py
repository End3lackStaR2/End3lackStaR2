#!/usr/bin/env python3
"""Tests for patch_kick_app.py (and the generated code) - run with: python -m unittest -v

They build a miniature copy of a Kick notifier app (the pre-patch shape, as
published in the MiniKick reference fix), patch it, and then *import the patched
files* to prove the rotation really works instead of only looking different.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import patch_kick_app as patcher  # noqa: E402

PROVIDER_PRE_PATCH = '''"""Pre-patch shape of backend/providers/chat/kick_ws_provider.py (test fixture)."""

import json
import logging

import websocket

logger = logging.getLogger("minikick.chat.kick")

RFC_6455_CLOSE_CODES = {1000: "Normal", 4001: "Unknown/Unregistered"}


class KickWebSocketManager:
    def __init__(self, cluster: str, key: str) -> None:
        self.cluster = cluster
        self.key = key
        self._running = False
        self.ws = None
        self._room_id = 0
        self._handlers = {
            "pusher:connection_established": self._handle_connection_established,
            "pusher:ping": self._handle_ping,
            "pusher:pong": self._handle_pong,
        }

    def start_socket(self, room_id: int) -> None:
        self._running = True
        self._room_id = room_id
        url = f"wss://ws-{self.cluster}.pusher.com/app/{self.key}?protocol=7&client=js&version=7.6.0"
        logger.info("[KickWebSocket] Connecting to Pusher WebSocket for room_id=%s...", room_id)
        self.ws = websocket.WebSocketApp(
            url,
            on_message=self._on_raw_frame,
            on_close=self._on_close,
        )

    def _on_close(self, _ws, status, msg) -> None:
        self._running = False
        meaning = RFC_6455_CLOSE_CODES.get(status, "Unknown") if status is not None else "Clean"
        logger.info("[KickWebSocket] WebSocket closed: code=%s (%s), reason=%s", status, meaning, msg or "N/A")

    def _on_raw_frame(self, ws, raw: str) -> None:
        payload = json.loads(raw)
        handler = self._handlers.get(payload.get("event"))
        if handler:
            data = payload.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    data = {"raw": data}
            handler(data, ws)

    def _handle_connection_established(self, inner, _ws) -> None:
        logger.info("[KickWebSocket] connected: %s", inner)

    def _handle_ping(self, _inner, ws) -> None:
        ws.send(json.dumps({"event": "pusher:pong", "data": {}}))

    def _handle_pong(self, _inner, _ws) -> None:
        return None

    def stop_socket(self) -> None:
        self._running = False
        logger.info("[KickWebSocket] Stopping Pusher socket...")
        if self.ws:
            self.ws.close()
'''

CONFIG_PRE_PATCH = {
    "channel": "some_streamer",
    "app_key": "32cbd69e4b950bf97679",
    "cluster": "us2",
    "log_level": "INFO",
}


def build_fixture(root: str) -> None:
    provider_dir = os.path.join(root, "backend", "providers", "chat")
    os.makedirs(provider_dir, exist_ok=True)
    os.makedirs(os.path.join(root, "backend", "config"), exist_ok=True)
    os.makedirs(os.path.join(root, "files"), exist_ok=True)
    with open(os.path.join(provider_dir, "kick_ws_provider.py"), "w", encoding="utf-8") as fh:
        fh.write(PROVIDER_PRE_PATCH)
    with open(os.path.join(root, "files", "config.json"), "w", encoding="utf-8") as fh:
        json.dump(CONFIG_PRE_PATCH, fh, indent=2)


def install_websocket_stub() -> None:
    if "websocket" in sys.modules:
        return
    stub = types.ModuleType("websocket")

    class WebSocketApp:  # noqa: D401 - tiny stub
        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs

        def close(self):
            return None

        def send(self, _payload):
            return None

    stub.WebSocketApp = WebSocketApp
    sys.modules["websocket"] = stub


def load_module(name: str, path: str, root: str):
    """Import a file as a module with `root` on sys.path (namespace packages)."""
    sys.path.insert(0, root)
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        try:
            sys.path.remove(root)
        except ValueError:
            pass


class PatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="kickfix_")
        build_fixture(self.root)
        install_websocket_stub()
        for mod in ("backend", "backend.config", "backend.config.pusher_config", "fixture_provider"):
            sys.modules.pop(mod, None)

    def tearDown(self) -> None:
        for mod in ("backend", "backend.config", "backend.config.pusher_config", "fixture_provider"):
            sys.modules.pop(mod, None)
        shutil.rmtree(self.root, ignore_errors=True)

    # -- data layer -------------------------------------------------------
    def test_plan_changes_nothing(self):
        before = open(os.path.join(self.root, "files", "config.json"), encoding="utf-8").read()
        rc = patcher.main(["--root", self.root, "--plan"])
        after = open(os.path.join(self.root, "files", "config.json"), encoding="utf-8").read()
        self.assertEqual(before, after)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "kick_pusher.json")))

    def test_dry_run_changes_nothing(self):
        patcher.main(["--root", self.root, "--dry-run"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "kick_pusher.json")))
        cfg = json.load(open(os.path.join(self.root, "files", "config.json"), encoding="utf-8"))
        self.assertEqual(cfg["app_key"], patcher.OLD_KEY)

    def test_full_patch_rewrites_key_file_and_config(self):
        rc = patcher.main(["--root", self.root])
        self.assertEqual(rc, 0)
        key_file = json.load(open(os.path.join(self.root, "kick_pusher.json"), encoding="utf-8"))
        self.assertEqual(key_file["schema"], "kick.pusher/v1")
        self.assertEqual([entry["key"] for entry in key_file["keys"]],
                         [patcher.NEW_KEY, patcher.OLD_KEY])
        self.assertEqual(key_file["cluster"], "us2")
        cfg = json.load(open(os.path.join(self.root, "files", "config.json"), encoding="utf-8"))
        self.assertEqual(cfg["app_key"], patcher.NEW_KEY)
        self.assertEqual(cfg["cluster"], "us2")
        self.assertEqual(cfg["channel"], "some_streamer", "unrelated config must be untouched")
        backups = [f for f in os.listdir(os.path.join(self.root, "files")) if ".bak-" in f]
        self.assertTrue(backups, "config.json must be backed up")

    def test_patch_is_idempotent(self):
        patcher.main(["--root", self.root])
        provider = os.path.join(self.root, "backend", "providers", "chat", "kick_ws_provider.py")
        first = open(provider, encoding="utf-8").read()
        patcher.main(["--root", self.root])
        second = open(provider, encoding="utf-8").read()
        self.assertEqual(first, second, "second run must not change the file again")
        self.assertEqual(second.count("def _rotate_key_if_rejected"), 1)

    def test_revert_restores_the_original(self):
        patcher.main(["--root", self.root])
        patcher.main(["--root", self.root, "--revert"])
        cfg = json.load(open(os.path.join(self.root, "files", "config.json"), encoding="utf-8"))
        self.assertEqual(cfg["app_key"], patcher.OLD_KEY)

    # -- code layer -------------------------------------------------------
    def test_patched_provider_parses_and_contains_rotation(self):
        patcher.main(["--root", self.root])
        provider = os.path.join(self.root, "backend", "providers", "chat", "kick_ws_provider.py")
        text = open(provider, encoding="utf-8").read()
        ast.parse(text)  # must be valid python
        self.assertIn("PUSHER_KEY_REJECTED_CODES", text)
        self.assertIn('"pusher:error": self._handle_pusher_error,', text)
        self.assertIn("def _rotate_key_if_rejected", text)
        self.assertIn("def _load_kick_pusher_config", text)
        self.assertIn("@property", text)
        self.assertNotIn("self.key = key", text)

    def test_generated_pusher_config_reads_the_key_file(self):
        patcher.main(["--root", self.root])
        module = load_module("backend.config.pusher_config",
                             os.path.join(self.root, "backend", "config", "pusher_config.py"),
                             self.root)
        self.assertEqual(module.KICK_PUSHER.cluster, "us2")
        self.assertEqual(module.KICK_PUSHER.keys, (patcher.NEW_KEY, patcher.OLD_KEY))
        self.assertTrue(all(module.PusherConfig is not None for _ in range(1)))

    def test_rotation_behaviour_of_the_patched_class(self):
        """The real proof: feed it pusher:error 4001 and watch the key change."""
        patcher.main(["--root", self.root])
        provider_path = os.path.join(self.root, "backend", "providers", "chat", "kick_ws_provider.py")
        module = load_module("fixture_provider", provider_path, self.root)
        manager = module.KickWebSocketManager("us2", patcher.OLD_KEY)
        self.assertEqual(manager.key, patcher.NEW_KEY,
                         "construction with the legacy key must expand to the full key list")

        manager._on_raw_frame(None, json.dumps({
            "event": "pusher:error",
            "data": json.dumps({"code": 4001, "message": "App key not in this cluster"}),
        }))
        manager._on_close(None, 4001, "App key not in this cluster")
        self.assertEqual(manager.key, patcher.OLD_KEY, "must rotate to the fallback key")

        manager._on_close(None, 1006, "socket dropped")
        self.assertEqual(manager.key, patcher.OLD_KEY, "an unrelated close must not rotate again")

        manager.stop_socket()
        self.assertEqual(manager.key, patcher.NEW_KEY, "stop_socket resets to the preferred key")

    def test_single_key_app_still_works_without_the_key_file(self):
        patcher.main(["--root", self.root, "--patch-code"])
        key_file = os.path.join(self.root, "kick_pusher.json")
        if os.path.exists(key_file):
            os.remove(key_file)
        for mod in list(sys.modules):
            if mod.startswith("backend"):
                sys.modules.pop(mod, None)
        provider_path = os.path.join(self.root, "backend", "providers", "chat", "kick_ws_provider.py")
        module = load_module("fixture_provider2", provider_path, self.root)
        manager = module.KickWebSocketManager("us2", "some-other-key")
        self.assertEqual(manager.key, "some-other-key")
        self.assertEqual(manager._keys, ("some-other-key",))

    def test_start_socket_uses_the_current_key_in_the_url(self):
        patcher.main(["--root", self.root, "--patch-code"])
        provider_path = os.path.join(self.root, "backend", "providers", "chat", "kick_ws_provider.py")
        module = load_module("fixture_provider3", provider_path, self.root)
        manager = module.KickWebSocketManager("us2", patcher.NEW_KEY)
        manager.start_socket(123)
        self.assertIn(f"/app/{patcher.NEW_KEY}?", manager.ws.url)
        manager._on_raw_frame(None, json.dumps({"event": "pusher:error", "data": {"code": 4003}}))
        manager._on_close(None, None, None)
        self.assertEqual(manager.key, patcher.OLD_KEY)
        manager.start_socket(123)
        self.assertIn(f"/app/{patcher.OLD_KEY}?", manager.ws.url)


if __name__ == "__main__":
    unittest.main(verbosity=2)
