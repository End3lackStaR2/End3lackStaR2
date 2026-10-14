#!/usr/bin/env python3
"""patch_kick_app.py - make a HermesAI / Kick notifier survive Pusher key rotations.

Two layers of repair, both idempotent, both backed up, both validated:

  1. DATA : kick_pusher.json   - the key list, primary first
            config.json        - legacy key -> current key, cluster -> us2
  2. CODE : backend/config/pusher_config.py              (created/updated)
            backend/providers/chat/kick_ws_provider.py   (patched) so the socket
            accepts several keys and rotates automatically when Pusher answers
            pusher:error code 4001/4003/4009 or closes with one of those codes.

The code patch is backwards compatible: the constructor still accepts a single
key, so no call site has to change. When the key it was handed belongs to the
known Kick key set, it upgrades itself to the full list.

Every write is preceded by a copy to <file>.bak-<timestamp>, and the result is
validated (json.loads / ast.parse) before the original is replaced.

Usage
-----
    python patch_kick_app.py --root D:\\HermesAI\\OBS --plan
    python patch_kick_app.py --root D:\\HermesAI\\OBS            # data + code (default)
    python patch_kick_app.py --root D:\\HermesAI\\OBS --dry-run
    python patch_kick_app.py --root D:\\HermesAI\\OBS --no-code  # only key file + config
    python patch_kick_app.py --root D:\\HermesAI\\OBS --revert

Exit codes: 0 = ok, 1 = changed nothing / warnings, 2 = failure.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
import textwrap
import time

NEW_KEY = "34bf7a0ff419a2a775b9"      # Kick's current Pusher app key
OLD_KEY = "32cbd69e4b950bf97679"      # revoked / being revoked
CLUSTER = "us2"
SOURCE = "Kick staff announcement x.com/notjoker 2026-10-08"

STAMP_FORMAT = "%Y%m%d-%H%M%S"


# -------------------------------------------------------------------------- output

def _c(code: str) -> str:
    if os.environ.get("NO_COLOR"):
        return ""
    if os.name == "nt" and not sys.stdout.isatty():
        return ""
    return code if sys.stdout.isatty() else ""


def say(kind: str, text: str) -> None:
    prefix = {"step": "\n== ", "info": "   ", "ok": "   [OK] ", "warn": "   [WARN] ",
              "fail": "   [FAIL] ", "dim": "   "}[kind]
    color = {"step": _c("\033[36m"), "ok": _c("\033[32m"), "warn": _c("\033[33m"),
             "fail": _c("\033[31m"), "dim": _c("\033[90m"), "info": ""}[kind]
    reset = _c("\033[0m")
    print(color + prefix + text + reset)


# -------------------------------------------------------------------------- files

def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return handle.read()


def write_text(path: str, text: str, bom: bool = False) -> None:
    folder = os.path.dirname(os.path.abspath(path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding=("utf-8-sig" if bom else "utf-8"), newline="") as handle:
        handle.write(text)


def backup(path: str) -> str:
    if not os.path.exists(path):
        return ""
    dst = f"{path}.bak-{time.strftime(STAMP_FORMAT)}"
    shutil.copy2(path, dst)
    return dst


def reindent(block: str, indent: str) -> str:
    """Move a dedented-source block to `indent`, keeping its relative depth."""
    dedented = textwrap.dedent(block.strip("\n"))
    lines = []
    for line in dedented.split("\n"):
        lines.append((indent + line) if line.strip() else "")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------- key material

def write_key_file(root: str, cluster: str, keys, dry_run: bool) -> str:
    path = os.path.join(root, "kick_pusher.json")
    entries = []
    for index, key in enumerate(keys):
        if index == 0:
            role = "primary"
        elif index == 1:
            role = "fallback"
        else:
            role = f"extra{index}"
        entries.append({"key": key, "cluster": cluster, "role": role})
    doc = {
        "schema": "kick.pusher/v1",
        "updated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": SOURCE,
        "cluster": cluster,
        "keys": entries,
    }
    if dry_run:
        say("dim", f"dry-run: would write {path}")
    else:
        write_text(path, json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        say("ok", f"key file: {path}")
    say("info", "     keys: " + ", ".join(keys))
    return path


def patch_config(config_path: str, new_key: str, cluster: str, dry_run: bool) -> list:
    changes: list = []
    if not config_path or not os.path.exists(config_path):
        say("warn", "no config.json found - nothing to patch (use --config)")
        return changes
    original = read_text(config_path)
    text = original

    if OLD_KEY in text:
        text = text.replace(OLD_KEY, new_key)
        changes.append(f"{OLD_KEY} -> {new_key}")

    key_field = re.compile(r'("(?:\w*app_?key|pusher[_-]?key|PUSHER_KEY|kick_?pusher_?key)"\s*:\s*")([^"]*)(")',
                           re.IGNORECASE)
    cluster_field = re.compile(r'("(?:\w*cluster|pusher[_-]?cluster|PUSHER_CLUSTER)"\s*:\s*")([^"]*)(")',
                               re.IGNORECASE)

    def set_key(match: "re.Match") -> str:
        name, value = match.group(1), match.group(2)
        if value != new_key:
            changes.append(name.strip('" :') + f" {value} -> {new_key}")
        return match.group(1) + new_key + match.group(3)

    def set_cluster(match: "re.Match") -> str:
        name, value = match.group(1), match.group(2)
        if value != cluster:
            changes.append(name.strip('" :') + f" {value} -> {cluster}")
        return match.group(1) + cluster + match.group(3)

    text = key_field.sub(set_key, text)
    text = cluster_field.sub(set_cluster, text)

    if text == original:
        say("ok", f"config.json is already correct ({new_key} / {cluster})")
        return changes

    try:
        json.loads(text)
    except ValueError as exc:
        say("fail", f"patched config.json would be invalid JSON ({exc}) - refusing to write")
        return []

    if dry_run:
        say("dim", f"dry-run: would patch {config_path} ({len(changes)} change(s))")
    else:
        bak = backup(config_path)
        write_text(config_path, text)
        say("ok", f"patched {config_path} (backup: {os.path.basename(bak) if bak else 'none'})")
    for change in changes:
        say("info", f"- {change}")
    return changes


# -------------------------------------------------------------------------- code patch

PUSHER_CONFIG_MODULE = '''"""Pusher credentials for Kick chat.

Generated by hermes/tools/patch_kick_app.py.

The key list is read from kick_pusher.json at import time when that file exists
(app root or <app root>/files), so a future Kick key rotation is a data change:
edit the json (or run hermes.ps1 fix) and restart - no code edit needed.

Order matters: the socket tries keys[0] first and rotates to the next key when
Pusher answers with pusher:error code 4001/4003/4009.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class PusherConfig:
    cluster: str
    keys: tuple


_DEFAULT = PusherConfig(
    cluster="{cluster}",
    keys=({keys}),
)


def _load_from_json():
    here = os.path.dirname(os.path.abspath(__file__))
    app_root = os.path.abspath(os.path.join(here, os.pardir, os.pardir))
    candidates = (
        os.path.join(app_root, "kick_pusher.json"),
        os.path.join(app_root, "files", "kick_pusher.json"),
        os.path.join(here, "kick_pusher.json"),
    )
    for candidate in candidates:
        try:
            with open(candidate, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:
            continue
        entries = data.get("keys") or []
        keys = []
        for entry in entries:
            if isinstance(entry, dict) and entry.get("key"):
                keys.append(str(entry["key"]))
            elif isinstance(entry, str):
                keys.append(entry)
        ordered = tuple(dict.fromkeys(k for k in keys if k))
        if not ordered:
            continue
        cluster = str(data.get("cluster") or _DEFAULT.cluster)
        return PusherConfig(cluster=cluster, keys=ordered)
    return None


KICK_PUSHER = _load_from_json() or _DEFAULT
'''

LOADER_FUNCTION = '''def _load_kick_pusher_config():
    """Best effort import of backend.config.pusher_config, whatever the layout is.

    Returns the PusherConfig (cluster + ordered key list) or None. Import
    failures must never break the socket, so every branch is guarded.
    """
    try:
        from backend.config.pusher_config import KICK_PUSHER
        return KICK_PUSHER
    except Exception:
        pass
    try:
        from ..config.pusher_config import KICK_PUSHER
        return KICK_PUSHER
    except Exception:
        pass
    try:
        import importlib.util
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        backend = os.path.dirname(os.path.dirname(here))
        path = os.path.join(backend, "config", "pusher_config.py")
        import sys as _sys
        spec = importlib.util.spec_from_file_location("kick_pusher_config_runtime", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        _sys.modules["kick_pusher_config_runtime"] = module  # dataclasses need this
        spec.loader.exec_module(module)
        return getattr(module, "KICK_PUSHER", None)
    except Exception:
        return None


'''

INIT_BODY = '''        self.cluster = cluster
        keys = None
        if isinstance(key, (list, tuple, set, frozenset)):
            keys = tuple(str(entry) for entry in key if entry)
        if not keys:
            known = _load_kick_pusher_config()
            if known is not None and cluster == getattr(known, "cluster", cluster) and str(key) in known.keys:
                keys = tuple(str(entry) for entry in known.keys)
        if not keys:
            keys = (str(key),)
        self._keys = tuple(dict.fromkeys(keys))
        self._key_index = 0
        self._pusher_error_code = None
'''

ROTATION_HELPERS = '''
    def _handle_pusher_error(self, inner, _ws=None):
        data = inner
        if isinstance(data, str):
            try:
                import json as _json
                data = _json.loads(data)
            except Exception:
                data = None
        if not isinstance(data, dict):
            data = {}
        code = data.get("code")
        self._pusher_error_code = code if isinstance(code, int) else None
        logger.error("[KickWebSocket] pusher:error code=%s message=%s", code, data.get("message"))

    def _rotate_key_if_rejected(self, close_status=None):
        code = self._pusher_error_code if self._pusher_error_code is not None else close_status
        self._pusher_error_code = None
        if code not in PUSHER_KEY_REJECTED_CODES:
            return False
        if self._key_index + 1 < len(self._keys):
            self._key_index += 1
            logger.warning(
                "[KickWebSocket] Pusher key rejected (code %s); switching to key %d/%d.",
                code, self._key_index + 1, len(self._keys),
            )
            return True
        logger.error("[KickWebSocket] Pusher key rejected (code %s) and there are no more keys to try.", code)
        return False
'''

PROPERTY_BLOCK = '''@property
def key(self) -> str:
    """The Pusher app key currently in use (rotates when Pusher rejects one)."""
    return self._keys[self._key_index]
'''


class Report:
    def __init__(self) -> None:
        self.applied: list = []
        self.skipped: list = []
        self.failed: list = []

    def ok(self, what: str) -> None:
        self.applied.append(what)
        say("ok", what)

    def skip(self, what: str) -> None:
        self.skipped.append(what)
        say("dim", f"not needed: {what}")

    def bad(self, what: str) -> None:
        self.failed.append(what)
        say("fail", what)


def find_block(text: str, name: str):
    """Offsets (start, end) of a `def name(...)` block at its own indentation."""
    match = re.search(rf"^(?P<indent>[ \t]*)def {re.escape(name)}\s*\(", text, re.M)
    if not match:
        return None
    indent = match.group("indent")
    tail = text[match.end():]
    end = len(text)
    for found in re.finditer(rf"^{indent}\S", tail, re.M):
        end = match.end() + found.start()
        break
    return match.start(), end


def find_signature(text: str, name: str):
    """(start, signature_end, block_end) for a def, signature on one line."""
    block = find_block(text, name)
    if not block:
        return None
    start, end = block
    newline = text.find("\n", start)
    if newline < 0 or newline > end:
        newline = end
    return start, newline, end


def patch_provider(path: str, dry_run: bool, report: Report) -> bool:
    original = read_text(path)
    if "_rotate_key_if_rejected" in original and "pusher:error" in original:
        report.skip("provider already rotates keys")
        return True
    text = original

    # 1. module level constant used by the rotation decision
    if "PUSHER_KEY_REJECTED_CODES" not in text:
        anchor = re.search(r"^(class\s+KickWebSocketManager\b)", text, re.M)
        if not anchor:
            report.bad("KickWebSocketManager class not found - no code patch attempted")
            return False
        insert = ("PUSHER_KEY_REJECTED_CODES = frozenset({4001, 4003, 4009})\n\n\n"
                  + LOADER_FUNCTION)
        text = text[:anchor.start()] + insert + text[anchor.start():]
        report.ok("added PUSHER_KEY_REJECTED_CODES and the config loader")

    # 2. constructor: keep accepting one key, add the multi-key state
    sig = find_signature(text, "__init__")
    if not sig:
        report.bad("KickWebSocketManager.__init__ not found - no code patch attempted")
        return False
    start, sig_end, block_end = sig
    signature = text[start:sig_end]
    if "key" not in signature and "keys" not in signature:
        report.bad(f"unexpected constructor signature: {signature.strip()!r} - no code patch attempted")
        return False
    body = text[sig_end:block_end]
    indent = re.match(r"^([ \t]*)", signature).group(1)
    new_state = reindent(INIT_BODY, indent + "    ").rstrip("\n")  # method body: one level in
    rebuilt_body = []
    inserted = False
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped in ("self.key = key", "self.key: str = key"):
            continue  # replaced by the multi key state below
        if stripped in ("self.cluster = cluster", "self.cluster: str = cluster"):
            rebuilt_body.append(new_state)  # INIT_BODY sets self.cluster itself
            inserted = True
            continue
        rebuilt_body.append(line)
    if not inserted:
        rebuilt_body.append("")
        rebuilt_body.append(new_state)
    prop = reindent(PROPERTY_BLOCK, indent)
    text = text[:start] + prop + "\n" + signature + "\n".join(rebuilt_body) + text[block_end:]
    report.ok("constructor accepts several keys (a single key still works)")

    # 3. register the pusher:error handler
    if "self._handle_pusher_error" not in text:
        anchor = re.search(r"^([ \t]*)\"pusher:pong\"\s*:\s*self\._handle_pong,\s*$", text, re.M)
        if anchor:
            text = (text[:anchor.end()] + f"\n{anchor.group(1)}\"pusher:error\": self._handle_pusher_error,"
                    + text[anchor.end():])
            report.ok("registered the pusher:error event handler")
        else:
            report.skip("pusher event table not recognised, pusher:error not registered")

    # 4. reset state + log which key is used when a socket starts
    if "len(self._keys)" not in text:
        anchor = re.search(r"^([ \t]*)self\.ws\s*=\s*websocket\.WebSocketApp\(", text, re.M)
        if anchor:
            indent = anchor.group(1)
            insert = (f"{indent}self._pusher_error_code = None\n"
                      f"{indent}logger.info(\n"
                      f"{indent}    \"[KickWebSocket] Connecting to Pusher WebSocket for room_id=%s (Pusher key %d/%d)...\",\n"
                      f"{indent}    room_id, self._key_index + 1, len(self._keys),\n"
                      f"{indent})\n")
            text = text[:anchor.start()] + insert + text[anchor.start():]
            report.ok("start_socket logs and resets the key state")

    # 5. rotate when the socket closes with a rejection code
    on_close = find_block(text, "_on_close")
    if on_close and "_rotate_key_if_rejected(status" not in text[on_close[0]:on_close[1]]:
        block_start, block_end = on_close
        block = text[block_start:block_end].rstrip("\n")
        indent = re.match(r"^([ \t]*)", text[block_start:]).group(1) + "    "
        text = (text[:block_start] + block + f"\n{indent}self._rotate_key_if_rejected(status)\n"
                + text[block_end:])
        report.ok("_on_close rotates the key when Pusher rejects it")

    # 6. the two helper methods
    if "_rotate_key_if_rejected(self" not in text:
        anchor = re.search(r"^([ \t]*)def _on_raw_frame\(", text, re.M)
        if not anchor:
            report.bad("_on_raw_frame not found to anchor the new methods - no code patch attempted")
            return False
        text = text[:anchor.start()] + reindent(ROTATION_HELPERS, anchor.group(1)) + "\n" + text[anchor.start():]
        report.ok("added _handle_pusher_error and _rotate_key_if_rejected")

    # 7. reset to the preferred key when the socket is stopped
    stop = find_block(text, "stop_socket")
    if stop and "_key_index = 0" not in text[stop[0]:stop[1]]:
        block = text[stop[0]:stop[1]]
        line = re.search(r"^([ \t]*)self\._running\s*=\s*False\s*$", block, re.M)
        if line:
            index = stop[0] + line.end()
            text = text[:index] + f"\n{line.group(1)}self._key_index = 0" + text[index:]
            report.ok("stop_socket resets to the preferred key")

    try:
        ast.parse(text)
    except SyntaxError as exc:
        report.bad(f"patched provider does not parse ({exc}) - original kept")
        return False

    if dry_run:
        say("dim", f"dry-run: would rewrite {path}")
    else:
        bak = backup(path)
        write_text(path, text)
        say("ok", f"patched {path} (backup: {os.path.basename(bak) if bak else 'none'})")
    return True


def patch_code(root: str, dry_run: bool, report: Report) -> Report:
    config_dir = os.path.join(root, "backend", "config")
    provider = os.path.join(root, "backend", "providers", "chat", "kick_ws_provider.py")
    module_path = os.path.join(config_dir, "pusher_config.py")

    if not os.path.exists(provider):
        report.skip(f"no Kick provider at {provider} (this app layout needs data patch only)")
        return report

    module_text = PUSHER_CONFIG_MODULE.format(cluster=CLUSTER, keys=f'"{NEW_KEY}", "{OLD_KEY}"')
    try:
        ast.parse(module_text)
    except SyntaxError as exc:  # pragma: no cover - template must stay valid
        report.bad(f"generated pusher_config.py does not parse: {exc}")
        return report

    if os.path.exists(module_path) and NEW_KEY in read_text(module_path):
        report.skip("backend/config/pusher_config.py already carries the new key")
    else:
        if dry_run:
            say("dim", f"dry-run: would write {module_path}")
        else:
            os.makedirs(config_dir, exist_ok=True)
            if os.path.exists(module_path):
                backup(module_path)
            write_text(module_path, module_text)
            say("ok", f"wrote {module_path}")

    patch_provider(provider, dry_run, report)
    return report


# -------------------------------------------------------------------------- discovery

def discover_root(start: str) -> str:
    current = os.path.abspath(start)
    for _ in range(4):
        if (os.path.isdir(os.path.join(current, "backend"))
                or os.path.exists(os.path.join(current, "files", "config.json"))
                or os.path.exists(os.path.join(current, "config.json"))):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return os.path.abspath(start)


def find_config(root: str) -> str:
    for candidate in (os.path.join(root, "files", "config.json"),
                      os.path.join(root, "config.json"),
                      os.path.join(root, "backend", "config.json")):
        if os.path.exists(candidate):
            return candidate
    return ""


def find_log(root: str) -> str:
    for candidate in (os.path.join(root, "files", "kick_notifier.log"),
                      os.path.join(root, "kick_notifier.log")):
        if os.path.exists(candidate):
            return candidate
    hits = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".venv", "venv", "node_modules", ".git", "__pycache__")]
        for name in files:
            if name.endswith(".log"):
                hits.append(os.path.join(base, name))
    hits.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return hits[0] if hits else ""


def revert(root: str, dry_run: bool) -> int:
    restored = 0
    for path in (os.path.join(root, "config.json"),
                 os.path.join(root, "files", "config.json"),
                 os.path.join(root, "backend", "config", "pusher_config.py"),
                 os.path.join(root, "backend", "providers", "chat", "kick_ws_provider.py")):
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            continue
        prefix = os.path.basename(path) + ".bak-"
        baks = sorted((os.path.join(folder, name) for name in os.listdir(folder)
                       if name.startswith(prefix)), reverse=True)
        if not baks:
            continue
        if dry_run:
            say("dim", f"dry-run: would restore {path} from {os.path.basename(baks[0])}")
        else:
            shutil.copy2(baks[0], path)
            say("ok", f"restored {path} from {os.path.basename(baks[0])}")
        restored += 1
    if not restored:
        say("warn", "nothing to revert (no .bak-<timestamp> files found)")
    return 0 if restored else 1


def plan(root: str) -> None:
    for label, path in (("config", find_config(root) or os.path.join(root, "files", "config.json")),
                        ("key file", os.path.join(root, "kick_pusher.json")),
                        ("log", find_log(root)),
                        ("pusher_config.py", os.path.join(root, "backend", "config", "pusher_config.py")),
                        ("provider", os.path.join(root, "backend", "providers", "chat", "kick_ws_provider.py"))):
        exists = os.path.exists(path)
        say("info" if exists else "dim", f"{label:17s} [{'found' if exists else 'missing'}] {path}")
    config_path = find_config(root)
    if config_path:
        try:
            flat = read_text(config_path)
            json.loads(flat)
            state = "OLD KEY" if OLD_KEY in flat else ("new key" if NEW_KEY in flat else "no key field found")
            say("info", f"config.json key state: {state}")
        except ValueError as exc:
            say("warn", f"config.json does not parse: {exc}")
    provider = os.path.join(root, "backend", "providers", "chat", "kick_ws_provider.py")
    if os.path.exists(provider):
        text = read_text(provider)
        say("info", f"provider rotation support: {'yes' if '_rotate_key_if_rejected' in text else 'no'}")


# -------------------------------------------------------------------------- cli

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Repair Kick Pusher key handling (data + code).")
    parser.add_argument("--root", default=".", help="app root, e.g. D:\\HermesAI\\OBS")
    parser.add_argument("--roots", nargs="+", help="patch several app roots at once")
    parser.add_argument("--config", default="", help="explicit config.json path")
    parser.add_argument("--key", default=NEW_KEY)
    parser.add_argument("--legacy-key", default=OLD_KEY)
    parser.add_argument("--cluster", default=CLUSTER)
    parser.add_argument("--patch-code", action="store_true", help="code patch only")
    parser.add_argument("--no-code", action="store_true", help="key file + config only")
    parser.add_argument("--plan", action="store_true", help="show what is there, change nothing")
    parser.add_argument("--revert", action="store_true", help="restore the newest .bak files")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    do_code = args.patch_code or not args.no_code
    do_data = not args.patch_code
    status = 0

    for raw_root in (args.roots or [args.root]):
        root = discover_root(raw_root)
        say("step", f"app root: {root}")
        if args.plan:
            plan(root)
            continue
        if args.revert:
            status = max(status, revert(root, args.dry_run))
            continue
        if do_data:
            write_key_file(root, args.cluster, [args.key, args.legacy_key], args.dry_run)
            patch_config(args.config or find_config(root), args.key, args.cluster, args.dry_run)
        if do_code:
            report = patch_code(root, args.dry_run, Report())
            if report.failed:
                status = max(status, 2)
        say("ok" if not args.dry_run else "warn",
            "done - restart the notifier so it picks up the new key"
            if not args.dry_run else "dry-run: nothing was written")
    return status


if __name__ == "__main__":
    sys.exit(main())
