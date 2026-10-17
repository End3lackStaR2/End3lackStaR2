#!/usr/bin/env python3
"""friday_bus.py - Friday (your machine) works with Arena (cloud) through a git repo.

How it works
------------
Arena cannot call into your machine (no MCP connector, no inbound access). So the
repo itself is the mailbox:

    Arena writes   bus/queue/T-*.json        (a task)
    Friday  writes bus/claimed/T-*.json      (claim, so only one Friday runs it)
    Friday  writes bus/results/T-*.json      (exit code, output tail, timing)
    Friday  writes bus/status/friday.status.json (heartbeat)
    Friday  writes bus/requests/Q-*.json     (a question for Arena)
    Arena writes   bus/answers/Q-*.json      (the answer)

Safety model (read bus/PROTOCOL.md): a task is UNTRUSTED INPUT. Friday only runs
actions listed in your local bus/actions.json, every parameter is validated
against a pattern, commands are executed without a shell (no "," "&&" "|"), each
action has a timeout, and potentially secret output is redacted before pushing.
Set "read_only": true in bus.config.json to make Friday never execute anything.

Typical use
-----------
    python friday_bus.py init --dir D:\\hermes-bus          # first time (clones)
    python friday_bus.py status                            # heartbeat to Arena
    python friday_bus.py once                              # do one task, if any
    python friday_bus.py loop                              # keep polling
    python friday_bus.py list                              # queues + latest results
    python friday_bus.py submit --action hermes_doctor      # you queue a task yourself
    python friday_bus.py ask "why 4001?"                    # ask Arena a question
    python friday_bus.py selftest                           # offline self test

Requires: python 3.8+ and git. Nothing else (no pip install).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION = "1.0.0"
SCHEMA_TASK = "friday.task/v1"
SCHEMA_RESULT = "friday.result/v1"
SCHEMA_REQUEST = "friday.request/v1"
SCHEMA_ANSWER = "friday.answer/v1"
SCHEMA_CONFIG = "friday.bus.config/v1"
SCHEMA_ACTIONS = "friday.actions/v1"

BUS_SUBDIR = "bus"
DEFAULT_REPO = "https://github.com/End3lackStaR2/End3lackStaR2.git"
DEFAULT_BRANCH = "arena/a508b692-end3lackstar2"
DEFAULT_MAX_OUTPUT = 32768
DEFAULT_LEASE = 900
DEFAULT_INTERVAL = 20

# a parameter value must look like a path/word; no quotes, no shell metacharacters
SAFE_PARAM = re.compile(r"^[A-Za-z0-9_.\-\\/: ]+$")

SECRET_PATTERNS = [
    (re.compile(r"(?i)(\"?(?:access_token|refresh_token|id_token|client_secret|api_key|apikey|password|secret|authorization|webhook)\"?\s*[:=]\s*\")([^\"]{4,})(\")"), r"\1<redacted>\3"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{10,}"), "Bearer <redacted>"),
    (re.compile(r"(?i)\bgh[pousr]_[A-Za-z0-9]{20,}"), "<redacted-github-token>"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"), "<redacted-jwt>"),
    (re.compile(r"(?i)\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "<redacted-slack-token>"),
    (re.compile(r"(?i)\bhttps://discord(app)?\.com/api/webhooks/\d+/[A-Za-z0-9_\-]+"), "<redacted-discord-webhook>"),
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def redact(text: str) -> str:
    if not text:
        return ""
    out = text
    for pattern, repl in SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def tail_text(text: str, limit: int) -> tuple:
    """Return (tail, truncated). Keeps the END of the text (that is where errors are)."""
    data = text.encode("utf-8", "replace")
    if len(data) <= limit:
        return text, False
    return data[-limit:].decode("utf-8", "replace"), True


def log(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(message, flush=True)


# --------------------------------------------------------------------------- io

def load_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except ValueError as exc:
        raise SystemExit(f"not valid JSON: {path} ({exc})")


def save_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(doc, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def new_id(prefix: str) -> str:
    return f"{prefix}-{utcnow().strftime('%Y%m%d')}-{int(time.time() * 1000) % 100000:05d}"


# --------------------------------------------------------------------------- git

class Git:
    def __init__(self, workdir: Path, identity: dict, quiet: bool = False):
        self.dir = workdir
        self.identity = identity or {}
        self.quiet = quiet

    def run(self, args, check=True, capture=True):
        cmd = ["git"]
        if self.identity.get("name"):
            cmd += ["-c", f"user.name={self.identity['name']}"]
        if self.identity.get("email"):
            cmd += ["-c", f"user.email={self.identity['email']}"]
        cmd += list(args)
        proc = subprocess.run(cmd, cwd=str(self.dir), capture_output=capture, text=True,
                              encoding="utf-8", errors="replace")
        if check and proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): "
                               f"{(proc.stderr or proc.stdout or '').strip()[:500]}")
        return proc

    def commit(self, message: str) -> bool:
        self.run(["add", "-A", "--", "."], check=False)
        proc = self.run(["commit", "-m", message], check=False)
        return proc.returncode == 0

    def push(self) -> bool:
        proc = self.run(["push", "origin", "HEAD"], check=False)
        return proc.returncode == 0


# --------------------------------------------------------------------------- bus

class Bus:
    def __init__(self, config: dict, root: Path, actions: dict, quiet: bool = False):
        self.cfg = config
        self.root = root                      # clone dir
        self.bus = root / BUS_SUBDIR
        self.actions = actions or {}
        self.quiet = quiet
        self.git = Git(root, config.get("git_identity") or {})
        self.branch = config.get("branch") or DEFAULT_BRANCH
        self.lease = int(config.get("lease_seconds") or DEFAULT_LEASE)
        self.max_output = int(config.get("max_output_bytes") or DEFAULT_MAX_OUTPUT)
        self.read_only = bool(config.get("read_only"))
        self.local = root / BUS_SUBDIR / ".local"
        self.local.mkdir(parents=True, exist_ok=True)
        self.tasks_done = 0
        self.tasks_failed = 0

    # -- paths
    def rel(self, *parts) -> str:
        return "/".join([BUS_SUBDIR, *parts])

    def path(self, *parts) -> Path:
        return self.bus.joinpath(*parts)

    # -- sync
    def sync(self) -> None:
        self.git.run(["fetch", "origin", self.branch])
        self.git.run(["checkout", "-B", self.branch, f"origin/{self.branch}"], check=False)
        self.git.run(["reset", "--hard", f"origin/{self.branch}"])
        # reset --hard leaves untracked files behind (e.g. leftovers of a failed push),
        # and those would break `git mv` later. Clean ONLY the bus-managed folders, so
        # actions.json / bus.config.json / .local (ignored, local only) are never touched.
        managed = ["queue", "claimed", "results", "done", "status", "requests", "answers"]
        self.git.run(["clean", "-fd", "--", *[self.rel(d) for d in managed]], check=False)

    def publish(self, message: str) -> bool:
        """commit + push, retrying after a rebase when someone else pushed first."""
        self.git.commit(message)
        for attempt in range(3):
            if self.git.push():
                return True
            self.git.run(["fetch", "origin", self.branch], check=False)
            rebase = self.git.run(["rebase", f"origin/{self.branch}"], check=False)
            if rebase.returncode != 0:
                self.git.run(["rebase", "--abort"], check=False)
                return False
        return False

    # -- queue
    def queued_tasks(self):
        out = []
        for path in sorted(self.path("queue").glob("*.json")):
            doc = load_json(path, {})
            if isinstance(doc, dict):
                doc["_file"] = path.name
                out.append(doc)
        out.sort(key=lambda d: (d.get("created_at_utc") or "", d.get("_file") or ""))
        return out

    def claimed_files(self):
        return sorted(self.path("claimed").glob("*.json"))

    def results_for(self, task_id: str):
        return self.path("results", f"{task_id}.json")

    def recover_stale_claims(self) -> list:
        """Move claims whose lease expired (and that have no result) back to the queue."""
        recovered = []
        now = utcnow()
        for path in self.claimed_files():
            doc = load_json(path, {}) or {}
            task_id = doc.get("id") or path.stem
            if self.results_for(task_id).exists():
                continue
            claim = doc.get("claim") or {}
            expires = claim.get("lease_expires_utc")
            try:
                expiry = datetime.strptime(expires, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                expiry = now - timedelta(seconds=1)
            if expiry > now:
                continue
            doc.pop("claim", None)
            doc["reclaims"] = int(doc.get("reclaims") or 0) + 1
            doc["last_reclaim_utc"] = iso(now)
            save_json(self.path("queue", path.name), doc)
            path.unlink()
            recovered.append(task_id)
        if recovered:
            self.publish("bus: requeue stale claim(s) " + ", ".join(recovered))
        return recovered

    def claim(self, task: dict) -> bool:
        """git mv queue -> claimed with a lease; push immediately so nobody else takes it."""
        name = task["_file"]
        target = self.path("claimed", name)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.git.run(["mv", self.rel("queue", name), self.rel("claimed", name)])
        except RuntimeError as exc:
            log(f"  cannot claim {name}: {str(exc)[:160]}", self.quiet)
            self.sync()
            return False
        claim = {
            "owner": self.cfg.get("owner") or "friday",
            "host": self.cfg.get("host") or socket.gethostname(),
            "pid": os.getpid(),
            "claimed_at_utc": iso(utcnow()),
            "lease_expires_utc": iso(utcnow() + timedelta(seconds=self.lease)),
        }
        doc = {k: v for k, v in task.items() if not k.startswith("_")}
        doc["claim"] = claim
        save_json(target, doc)
        if self.publish(f"bus: claim {doc.get('id')}"):
            return True
        self.sync()   # somebody else got it (or we are behind) - back off
        return False

    # -- actions
    def validate_params(self, action: str, spec: dict, params: dict) -> tuple:
        """Return (clean_params, error). Rejects anything that is not a plain path/word."""
        wanted = spec.get("params") or {}
        clean = {}
        for name, rule in wanted.items():
            value = params.get(name, rule.get("default"))
            if value is None:
                if rule.get("required"):
                    return {}, f"missing required param: {name}"
                continue
            value = str(value)
            pattern = rule.get("pattern")
            if pattern:
                if not re.match(pattern, value):
                    return {}, f"param {name} does not match {pattern}"
            elif not SAFE_PARAM.match(value):
                return {}, f"param {name} contains unsupported characters: {value[:40]!r}"
            clean[name] = value
        extra = set(params) - set(wanted)
        if extra and not spec.get("allow_extra_params"):
            return {}, f"unexpected param(s): {', '.join(sorted(extra))}"
        return clean, None

    def resolve(self, spec: dict, params: dict) -> tuple:
        """Build argv without ever going through a shell."""
        builtins = {
            "repo_dir": str(self.root),
            "bus_dir": str(self.bus),
            "default_app_dir": str(self.cfg.get("default_app_dir") or ""),
            "hermes_dir": str(self.cfg.get("hermes_dir") or ""),
        }
        values = {**builtins, **params}

        def fill(text: str) -> str:
            def repl(match):
                key = match.group(1)
                if key not in values:
                    raise KeyError(key)
                return str(values[key])
            return re.sub(r"\{([A-Za-z0-9_]+)\}", repl, text)

        program = fill(spec["program"])
        args = [fill(a) for a in spec.get("args") or []]
        cwd = fill(spec.get("cwd") or str(self.root))
        return program, args, cwd

    def execute(self, task: dict) -> dict:
        task_id = task.get("id") or "unknown"
        action = task.get("action") or ""
        params = task.get("params") or {}
        started = utcnow()
        result = {
            "schema": SCHEMA_RESULT,
            "id": task_id,
            "action": action,
            "status": "error",
            "exit_code": None,
            "duration_ms": 0,
            "started_at_utc": iso(started),
            "finished_at_utc": None,
            "host": self.cfg.get("host") or socket.gethostname(),
            "friday_version": VERSION,
            "stdout_tail": "",
            "stderr_tail": "",
            "truncated": False,
            "local_output_file": "",
        }

        if self.read_only:
            result["status"] = "refused"
            result["stderr_tail"] = "bus.config.json has read_only=true - Friday does not execute tasks"
            result["finished_at_utc"] = iso(utcnow())
            return result

        spec = self.actions.get(action)
        if not spec:
            result["status"] = "refused"
            result["stderr_tail"] = (f"action {action!r} is not in bus/actions.json "
                                     f"(allowed: {', '.join(sorted(self.actions)) or 'none'})")
            result["finished_at_utc"] = iso(utcnow())
            return result

        clean, error = self.validate_params(action, spec, params)
        if error:
            result["status"] = "invalid"
            result["stderr_tail"] = error
            result["finished_at_utc"] = iso(utcnow())
            return result

        try:
            program, args, cwd = self.resolve(spec, clean)
        except KeyError as exc:
            result["status"] = "invalid"
            result["stderr_tail"] = f"unknown placeholder {{{exc.args[0]}}} in the action definition"
            result["finished_at_utc"] = iso(utcnow())
            return result

        timeout = int(spec.get("timeout_seconds") or 300)
        log(f"  running: {program} {' '.join(args)}   (cwd={cwd}, timeout={timeout}s)", self.quiet)
        try:
            proc = subprocess.Popen([program, *args], cwd=cwd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                    errors="replace", shell=False)
            try:
                out, err = proc.communicate(timeout=timeout)
                result["exit_code"] = proc.returncode
                result["status"] = "ok" if proc.returncode == 0 else "failed"
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   capture_output=True, check=False)
                else:
                    proc.kill()
                out, err = proc.communicate()
                result["status"] = "timeout"
                err = (err or "") + f"\n[friday] action exceeded {timeout}s and was killed"
        except FileNotFoundError:
            result["status"] = "error"
            out, err = "", f"program not found: {program}"
        except Exception as exc:  # noqa: BLE001 - always report, never crash a polling loop
            result["status"] = "error"
            out, err = "", f"{type(exc).__name__}: {exc}"

        out = redact(out or "")
        err = redact(err or "")
        out_file = self.local / f"{task_id}.out.txt"
        err_file = self.local / f"{task_id}.err.txt"
        try:
            out_file.write_text(out, encoding="utf-8")
            err_file.write_text(err, encoding="utf-8")
        except OSError:
            pass
        out_tail, cut1 = tail_text(out, self.max_output)
        err_tail, cut2 = tail_text(err, self.max_output)
        result["stdout_tail"] = out_tail
        result["stderr_tail"] = err_tail
        result["truncated"] = bool(cut1 or cut2)
        result["local_output_file"] = str(out_file)
        result["duration_ms"] = int((utcnow() - started).total_seconds() * 1000)
        result["finished_at_utc"] = iso(utcnow())
        return result

    def complete(self, task: dict, result: dict) -> bool:
        task_id = task.get("id") or "unknown"
        save_json(self.results_for(task_id), result)
        claimed = self.path("claimed", task["_file"])
        done = self.path("done", task["_file"])
        done.parent.mkdir(parents=True, exist_ok=True)
        archive = {k: v for k, v in task.items() if not k.startswith("_")}
        archive["final_status"] = result.get("status")
        archive["finished_at_utc"] = result.get("finished_at_utc")
        archive["duration_ms"] = result.get("duration_ms")
        save_json(done, archive)
        if claimed.exists():
            claimed.unlink()
        return self.publish(f"bus: result {task_id} ({result.get('status')})")

    # -- one poll
    def poll_once(self, dry_run: bool = False) -> str:
        """Returns one of: idle | done | lost-race | refused | error."""
        self.sync()
        if self.read_only:
            log("  read_only=true: pulling only, not claiming or running anything", self.quiet)
            return "read-only"
        recovered = self.recover_stale_claims()
        for task_id in recovered:
            log(f"  requeued stale claim {task_id}", self.quiet)

        tasks = self.queued_tasks()
        if not tasks:
            return "idle"

        task = tasks[0]
        if dry_run:
            spec = self.actions.get(task.get("action") or "")
            log(f"  dry-run: next task {task.get('id')} action={task.get('action')} "
                f"allowed={'yes' if spec else 'NO (would be refused)'}", self.quiet)
            if spec:
                try:
                    prog, args, cwd = self.resolve(spec, self.validate_params(task.get("action"), spec,
                                                                              task.get("params") or {})[0])
                    log(f"  would run: {prog} {' '.join(args)}  (cwd={cwd})", self.quiet)
                except Exception as exc:  # noqa: BLE001
                    log(f"  cannot preview: {exc}", self.quiet)
            return "idle"

        log(f"  task {task.get('id')} action={task.get('action')}", self.quiet)
        if not self.claim(task):
            log("  lost the race for this task (another Friday claimed it) - syncing", self.quiet)
            return "lost-race"

        result = self.execute(task)
        ok = self.complete(task, result)
        if result.get("status") == "ok":
            self.tasks_done += 1
        else:
            self.tasks_failed += 1
        if not ok:
            log("  warning: result was written locally but the push failed - will retry next poll", self.quiet)
        log(f"  {task.get('id')} -> {result.get('status')} (exit {result.get('exit_code')}, "
            f"{result.get('duration_ms')} ms)", self.quiet)
        return "refused" if result.get("status") in ("refused", "invalid") else "done"

    # -- status / list
    def status_doc(self, note: str = "") -> dict:
        status_path = self.path("status", "friday.status.json")
        previous = load_json(status_path, {}) or {}
        doc = {
            "schema": "friday.status/v1",
            "owner": self.cfg.get("owner") or "friday",
            "host": self.cfg.get("host") or socket.gethostname(),
            "friday_version": VERSION,
            "python": sys.version.split()[0],
            "pid": os.getpid(),
            "read_only": self.read_only,
            "branch": self.branch,
            "last_poll_utc": iso(utcnow()),
            "first_seen_utc": previous.get("first_seen_utc") or iso(utcnow()),
            "polls": int(previous.get("polls") or 0) + 1,
            "tasks_done": int(previous.get("tasks_done") or 0) + self.tasks_done,
            "tasks_failed": int(previous.get("tasks_failed") or 0) + self.tasks_failed,
            "actions": sorted(self.actions),
            "note": note or previous.get("note") or "",
        }
        return doc

    def write_status(self, note: str = "") -> bool:
        doc = self.status_doc(note)
        save_json(self.path("status", "friday.status.json"), doc)
        return self.publish("bus: status heartbeat")


# --------------------------------------------------------------------------- config

def config_path(root: Path) -> Path:
    return root / BUS_SUBDIR / "bus.config.json"


def actions_path(root: Path) -> Path:
    return root / BUS_SUBDIR / "actions.json"


def make_default_config(repo: str, branch: str) -> dict:
    return {
        "schema": SCHEMA_CONFIG,
        "repo_url": repo,
        "branch": branch,
        "owner": "friday",
        "host": socket.gethostname(),
        "poll_interval_seconds": DEFAULT_INTERVAL,
        "lease_seconds": DEFAULT_LEASE,
        "max_output_bytes": DEFAULT_MAX_OUTPUT,
        "read_only": False,
        "default_app_dir": r"D:\HermesAI\OBS",
        "hermes_dir": str(Path.home() / "hermes-kick"),
        "git_identity": {"name": "friday-bus", "email": "friday-bus@localhost"},
    }


def load_config(root: Path, explicit: str = "") -> dict:
    path = Path(explicit) if explicit else config_path(root)
    cfg = load_json(path)
    if not isinstance(cfg, dict):
        raise SystemExit(f"config not found or empty: {path}\nRun: python friday_bus.py init --dir <dir>")
    return cfg


def load_actions(root: Path) -> dict:
    path = actions_path(root)
    doc = load_json(path, None)
    if doc is None:
        log(f"  note: {path} not found - every task will be refused (copy actions.json.example)")
        return {}
    return (doc.get("actions") or {}) if isinstance(doc, dict) else {}


# --------------------------------------------------------------------------- commands

def cmd_init(args) -> int:
    root = Path(args.dir).expanduser().resolve()
    repo = args.repo or DEFAULT_REPO
    branch = args.branch or DEFAULT_BRANCH
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        log(f"cloning {repo} -> {root}")
        proc = subprocess.run(["git", "clone", "--branch", branch, repo, str(root)],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            proc = subprocess.run(["git", "clone", repo, str(root)], capture_output=True, text=True)
            if proc.returncode != 0:
                print(proc.stderr.strip()[:500])
                return 2
            subprocess.run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=str(root),
                           capture_output=True, text=True, check=False)
    else:
        log(f"using existing clone: {root}")

    if not config_path(root).exists():
        save_json(config_path(root), make_default_config(repo, branch))
        log(f"wrote {config_path(root)}  <- edit default_app_dir / hermes_dir")
    example = root / BUS_SUBDIR / "actions.json.example"
    if not actions_path(root).exists():
        if example.exists():
            actions_path(root).write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            log(f"wrote {actions_path(root)} from the example  <- review it before running tasks")
        else:
            log("actions.json.example not found - create bus/actions.json by hand")
    for sub in ("queue", "claimed", "results", "done", "status", "requests", "answers"):
        (root / BUS_SUBDIR / sub).mkdir(parents=True, exist_ok=True)
    log("init done")
    return 0


def _bus_from_args(args) -> Bus:
    root = Path(args.dir).expanduser().resolve()
    cfg = load_config(root, args.config)
    return Bus(cfg, root, load_actions(root), quiet=args.quiet)


def cmd_once(args) -> int:
    bus = _bus_from_args(args)
    outcome = bus.poll_once(dry_run=args.dry_run)
    bus.write_status()
    log(f"poll: {outcome}")
    return 0 if outcome != "error" else 2


def cmd_loop(args) -> int:
    bus = _bus_from_args(args)
    interval = int(args.interval or bus.cfg.get("poll_interval_seconds") or DEFAULT_INTERVAL)
    log(f"loop: polling every {interval}s (Ctrl+C to stop)")
    bus.write_status(note="loop started")
    idle_rounds = 0
    try:
        while True:
            outcome = bus.poll_once(dry_run=args.dry_run)
            if outcome == "idle":
                idle_rounds += 1
                if idle_rounds % 5 == 0:
                    bus.write_status()
            else:
                idle_rounds = 0
                log(f"poll: {outcome}")
            time.sleep(interval)
    except KeyboardInterrupt:
        log("\nstopped")
        bus.write_status(note="loop stopped")
        return 0


def cmd_status(args) -> int:
    bus = _bus_from_args(args)
    bus.sync()
    doc = bus.status_doc(note=args.note or "")
    save_json(bus.path("status", "friday.status.json"), doc)
    if bus.publish("bus: status heartbeat"):
        log(json.dumps(doc, ensure_ascii=False, indent=2))
        log("heartbeat pushed")
        return 0
    log("heartbeat written locally but push failed (network/auth?)")
    return 1


def cmd_list(args) -> int:
    bus = _bus_from_args(args)
    bus.sync()
    queue = bus.queued_tasks()
    claimed = bus.claimed_files()
    results = sorted(bus.path("results").glob("*.json"),
                     key=lambda p: p.stat().st_mtime, reverse=True)[:10]
    log(f"queued   : {len(queue)}")
    for task in queue:
        log(f"  {task.get('id'):24s} {task.get('action'):20s} {task.get('created_at_utc')}  {task.get('notes') or ''}")
    log(f"claimed  : {len(claimed)}")
    for path in claimed:
        doc = load_json(path, {}) or {}
        claim = doc.get("claim") or {}
        log(f"  {doc.get('id'):24s} by {claim.get('host')} until {claim.get('lease_expires_utc')}")
    log(f"requests : {len(list(bus.path('requests').glob('*.json')))}")
    log("last results:")
    for path in results:
        doc = load_json(path, {}) or {}
        log(f"  {doc.get('id'):24s} {str(doc.get('status')):8s} exit={doc.get('exit_code')} "
            f"{doc.get('action')} ({doc.get('duration_ms')} ms)")
    return 0


def cmd_submit(args) -> int:
    bus = _bus_from_args(args)
    bus.sync()
    task_id = args.id or new_id("T")
    params = {}
    for item in args.param or []:
        if "=" not in item:
            print(f"bad --param {item!r}, expected name=value")
            return 2
        key, value = item.split("=", 1)
        params[key] = value
    doc = {
        "schema": SCHEMA_TASK,
        "id": task_id,
        "created_at_utc": iso(utcnow()),
        "created_by": bus.cfg.get("owner") or "friday",
        "action": args.action,
        "params": params,
        "lease_seconds": bus.lease,
        "notes": args.note or "",
        "reply_via": f"bus/results/{task_id}.json",
    }
    save_json(bus.path("queue", f"{task_id}.json"), doc)
    if bus.publish(f"bus: task {task_id} ({args.action})"):
        log(f"queued {task_id}: {args.action} {params}")
        return 0
    log("could not push the task")
    return 1


def cmd_ask(args) -> int:
    bus = _bus_from_args(args)
    bus.sync()
    request_id = new_id("Q")
    doc = {
        "schema": SCHEMA_REQUEST,
        "id": request_id,
        "created_at_utc": iso(utcnow()),
        "created_by": bus.cfg.get("owner") or "friday",
        "host": bus.cfg.get("host") or socket.gethostname(),
        "question": args.text,
        "context": args.context or "",
        "answer_file": f"bus/answers/{request_id}.json",
    }
    save_json(bus.path("requests", f"{request_id}.json"), doc)
    if bus.publish(f"bus: request {request_id}"):
        log(f"asked Arena: {request_id}")
        log("now tell Arena in chat: \"check the bus\" (Arena is not autonomous)")
        return 0
    log("could not push the request")
    return 1


def cmd_selftest(args) -> int:
    passes = 0
    fails = []

    def check(name, cond, detail=""):
        nonlocal passes
        if cond:
            passes += 1
            print(f"  [PASS] {name}")
        else:
            fails.append(f"{name} {detail}")
            print(f"  [FAIL] {name} {detail}")

    # redaction: secrets go, public pusher keys stay
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP"
    check("redacts jwt", "redacted-jwt" in redact(jwt))
    check("redacts bearer", "redacted" in redact("Authorization: Bearer abcdefghijklmno"))
    check("redacts github token", "redacted" in redact("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"))
    check("redacts discord webhook", "redacted" in redact("https://discord.com/api/webhooks/123/abc-DEF"))
    check("keeps public pusher key", "34bf7a0ff419a2a775b9" in redact("app_key=34bf7a0ff419a2a775b9"))

    # tail keeps the end
    tail, cut = tail_text("a" * 100, 10)
    check("tail keeps the end", tail == "a" * 10 and cut)

    # ids are unique-ish and stable in shape
    ids = {new_id("T"), new_id("T"), new_id("Q")}
    check("ids look right", all(re.match(r"^[TQ]-\d{8}-\d{5}$", i) for i in ids))

    # config template is complete
    cfg = make_default_config(DEFAULT_REPO, DEFAULT_BRANCH)
    needed = {"schema", "repo_url", "branch", "owner", "host", "poll_interval_seconds",
              "lease_seconds", "max_output_bytes", "read_only", "git_identity"}
    check("config template has every key", needed <= set(cfg))

    # param validation blocks shell metacharacters
    root = Path(args.dir).expanduser().resolve() if args.dir else Path.cwd()
    try:
        bus = Bus(make_default_config("x", "main"), root, {"t": {"program": "echo", "args": []}}, quiet=True)
        _, err = bus.validate_params("t", {"params": {"p": {"required": True}}}, {"p": "D:\\ok\\path 1"})
        check("accepts a plain path", err is None, str(err))
        _, err2 = bus.validate_params("t", {"params": {"p": {"required": True}}}, {"p": "x; rm -rf /"})
        check("rejects shell metacharacters", err2 is not None)
        _, err3 = bus.validate_params("t", {"params": {"n": {"required": True, "pattern": "^[0-9]{1,4}$"}}},
                                      {"n": "50"})
        check("honours custom pattern", err3 is None, str(err3))
        _, err4 = bus.validate_params("t", {"params": {"n": {"required": True, "pattern": "^[0-9]{1,4}$"}}},
                                      {"n": "50; ls"})
        check("custom pattern rejects junk", err4 is not None)
        _, err5 = bus.validate_params("t", {"params": {}}, {"sneaky": "1"})
        check("rejects unknown params", err5 is not None)
    except Exception as exc:  # noqa: BLE001
        fails.append(f"validation harness: {exc}")
        print(f"  [FAIL] validation harness: {exc}")

    print()
    print(f"self test: {passes} passed, {len(fails)} failed")
    for item in fails:
        print("  -", item)
    return 2 if fails else 0


# --------------------------------------------------------------------------- cli

def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=".", help="bus clone directory (where bus/ lives)")
    common.add_argument("--config", default="", help="explicit bus.config.json path")
    common.add_argument("--quiet", action="store_true")

    # The sub-parsers accept the same flags so both orders work
    # ("--dir X once" and "once --dir X"). SUPPRESS is required: without it the
    # sub-parser defaults would silently overwrite values given before the verb.
    after = argparse.ArgumentParser(add_help=False)
    after.add_argument("--dir", default=argparse.SUPPRESS)
    after.add_argument("--config", default=argparse.SUPPRESS)
    after.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(description="Friday <-> Arena task bus over git (stdlib only).",
                                     parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)
    add = lambda name, help_text: sub.add_parser(name, help=help_text, parents=[after])

    p = add("init", "clone the repo and create config/actions on first use")
    p.add_argument("--repo", default="")
    p.add_argument("--branch", default="")
    p.set_defaults(func=cmd_init)

    p = add("once", "do one task if there is one")
    p.add_argument("--dry-run", action="store_true", help="show what would run, claim nothing")
    p.set_defaults(func=cmd_once)

    p = add("loop", "keep polling")
    p.add_argument("--interval", type=int, default=0)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_loop)

    p = add("status", "write a heartbeat for Arena")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_status)

    p = add("list", "show queue, claims and latest results")
    p.set_defaults(func=cmd_list)

    p = add("submit", "queue a task yourself (same format Arena uses)")
    p.add_argument("--action", required=True)
    p.add_argument("--param", action="append")
    p.add_argument("--note", default="")
    p.add_argument("--id", default="")
    p.set_defaults(func=cmd_submit)

    p = add("ask", "write a question for Arena into bus/requests/")
    p.add_argument("text")
    p.add_argument("--context", default="")
    p.set_defaults(func=cmd_ask)

    p = add("selftest", "offline checks, touches nothing")
    p.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
