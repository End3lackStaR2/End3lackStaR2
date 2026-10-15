#!/usr/bin/env python3
"""End-to-end tests for friday_bus.py against a REAL git remote (a local bare repo).

What is proven here (no mocking of the protocol):
  * a task written by "Arena" is claimed, executed and answered by "Friday"
  * the result carries exit code, output and timing, and lands in bus/results/
  * unknown actions are refused and shell metacharacters in params are rejected
  * a task is never executed twice: a lost claim race aborts cleanly
  * a dead Friday's stale claim is requeued after its lease expires
  * read_only mode runs nothing at all
  * heartbeats and questions (bus/requests) reach the remote

Run:  python -m unittest -v bus.tests.test_bus_e2e     (or from bus/tests: python test_bus_e2e.py)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUS_DIR = HERE.parent                       # .../bus
REPO_ROOT = BUS_DIR.parent                  # repo root
FRIDAY = BUS_DIR / "friday_bus.py"

GIT_ID = ["-c", "user.name=bus-test", "-c", "user.email=bus-test@localhost"]


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", *GIT_ID, *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr[:400]}")
    return proc


def run_cli(directory, *args):
    """Run friday_bus.py exactly the way a user would."""
    return subprocess.run([sys.executable, str(FRIDAY), "--dir", str(directory), *args],
                          capture_output=True, text=True)


def utc(offset_seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class BusE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bus_e2e_"))
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))
        self.seed = self.tmp / "seed"
        git(self.tmp, "clone", str(self.origin), str(self.seed))
        self._populate(Path(self.seed))

        self.arena = self.tmp / "arena"          # plays the part of Arena (cloud)
        git(self.tmp, "clone", str(self.origin), str(self.arena))

        self.friday = self.tmp / "friday"        # plays the part of Friday (your machine)
        proc = run_cli(self.friday, "init", "--repo", str(self.origin), "--branch", "main")
        self.assertEqual(proc.returncode, 0, f"init failed: {proc.stdout} {proc.stderr}")
        actions = {
            "schema": "friday.actions/v1",
            "actions": {
                "echo_ok": {"program": sys.executable, "args": ["-c", "print('BUS-OK')"],
                            "timeout_seconds": 60, "params": {}},
                "echo_param": {"program": sys.executable,
                               "args": ["-c", "import sys;print('P=' + sys.argv[1])", "{value}"],
                               "timeout_seconds": 60,
                               "params": {"value": {"required": True}}},
                "num_only": {"program": sys.executable, "args": ["-c", "print('NUM')"],
                             "timeout_seconds": 60,
                             "params": {"n": {"required": True, "pattern": "^[0-9]{1,3}$"}}},
                "slow": {"program": sys.executable, "args": ["-c", "import time;time.sleep(30)"],
                         "timeout_seconds": 2, "params": {}},
            },
        }
        (self.friday / "bus" / "actions.json").write_text(json.dumps(actions, indent=2), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers
    def _populate(self, clone: Path):
        """Seed the bare repo with the bus skeleton (only trackable files)."""
        bus = clone / "bus"
        bus.mkdir(parents=True, exist_ok=True)
        for name in ("friday_bus.py", "actions.json.example", ".gitignore", "PROTOCOL.md"):
            src = BUS_DIR / name
            if src.exists():
                shutil.copy2(src, bus / name)
        for sub in ("queue", "claimed", "results", "done", "status", "requests", "answers"):
            (bus / sub).mkdir(exist_ok=True)
            (bus / sub / ".gitkeep").write_text("", encoding="utf-8")
        git(clone, "add", "-A")
        git(clone, "commit", "-m", "seed bus skeleton")
        git(clone, "push", "origin", "main")

    def queue_task(self, task_id, action, params=None, claims=None, reclaims=0):
        doc = {
            "schema": "friday.task/v1",
            "id": task_id,
            "created_at_utc": utc(-60),
            "created_by": "arena",
            "action": action,
            "params": params or {},
            "lease_seconds": 900,
            "notes": "test task",
        }
        if claims:
            doc["claim"] = claims
        if reclaims:
            doc["reclaims"] = reclaims
        target = self.arena / "bus" / "queue" / f"{task_id}.json"
        target.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        git(self.arena, "add", "-A")
        git(self.arena, "commit", "-m", f"task {task_id}")
        git(self.arena, "push", "origin", "main")

    def pull_origin(self, where=None):
        """Refresh the 'Arena' clone so we read exactly what Friday pushed."""
        target = where or self.arena
        git(target, "fetch", "origin", "main")
        git(target, "reset", "--hard", "origin/main")

    def read_remote(self, rel_path):
        self.pull_origin()
        path = self.arena / rel_path
        self.assertTrue(path.exists(), f"{rel_path} not found in the remote")
        return json.loads(path.read_text(encoding="utf-8"))

    def remote_exists(self, rel_path):
        self.pull_origin()
        return (self.arena / rel_path).exists()

    # -- tests
    def test_task_round_trip(self):
        self.queue_task("T-20260101-00001", "echo_ok")
        proc = run_cli(self.friday, "once")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        result = self.read_remote("bus/results/T-20260101-00001.json")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("BUS-OK", result["stdout_tail"])
        self.assertEqual(result["schema"], "friday.result/v1")
        self.assertTrue(result["duration_ms"] >= 0)

        self.assertTrue(self.remote_exists("bus/done/T-20260101-00001.json"))
        self.assertFalse(self.remote_exists("bus/queue/T-20260101-00001.json"))
        self.assertFalse(self.remote_exists("bus/claimed/T-20260101-00001.json"))

    def test_second_run_is_idle_and_never_repeats(self):
        self.queue_task("T-20260101-00002", "echo_ok")
        self.assertEqual(run_cli(self.friday, "once").returncode, 0)
        proc = run_cli(self.friday, "once")
        self.assertIn("poll: idle", proc.stdout)
        self.assertFalse(self.remote_exists("bus/queue/T-20260101-00002.json"))

    def test_unknown_action_is_refused(self):
        self.queue_task("T-20260101-00003", "rm_rf_everything", {"path": "/"})
        proc = run_cli(self.friday, "once")
        self.assertEqual(proc.returncode, 0)
        result = self.read_remote("bus/results/T-20260101-00003.json")
        self.assertEqual(result["status"], "refused")
        self.assertIn("not in bus/actions.json", result["stderr_tail"])
        self.assertIsNone(result["exit_code"])

    def test_shell_metacharacters_are_invalid(self):
        self.queue_task("T-20260101-00004", "echo_param", {"value": "x; rm -rf /"})
        run_cli(self.friday, "once")
        result = self.read_remote("bus/results/T-20260101-00004.json")
        self.assertEqual(result["status"], "invalid")
        self.assertIn("unsupported characters", result["stderr_tail"])

    def test_custom_pattern_allows_and_blocks(self):
        self.queue_task("T-20260101-00005", "num_only", {"n": "42"})
        run_cli(self.friday, "once")
        ok = self.read_remote("bus/results/T-20260101-00005.json")
        self.assertEqual(ok["status"], "ok")

        self.queue_task("T-20260101-00006", "num_only", {"n": "42; ls"})
        run_cli(self.friday, "once")
        bad = self.read_remote("bus/results/T-20260101-00006.json")
        self.assertEqual(bad["status"], "invalid")

    def test_timeout_is_reported(self):
        self.queue_task("T-20260101-00007", "slow")
        run_cli(self.friday, "once")
        result = self.read_remote("bus/results/T-20260101-00007.json")
        self.assertEqual(result["status"], "timeout")
        self.assertIn("exceeded 2s", result["stderr_tail"])

    def test_stale_local_copy_is_reconciled_not_double_run(self):
        """A Friday that is behind must NOT re-run a task somebody else already claimed.

        It syncs first, so the stale local copy disappears and the poll is a no-op.
        """
        stale_claim = {"owner": "friday-2", "host": "OTHER-PC", "pid": 1,
                       "claimed_at_utc": utc(-5), "lease_expires_utc": utc(600)}
        doc = {"schema": "friday.task/v1", "id": "T-20260101-00008", "action": "echo_ok", "params": {},
               "created_at_utc": utc(-60), "created_by": "arena", "lease_seconds": 900, "claim": stale_claim}
        (self.arena / "bus" / "claimed" / "T-20260101-00008.json").write_text(json.dumps(doc, indent=2),
                                                                             encoding="utf-8")
        git(self.arena, "add", "-A")
        git(self.arena, "commit", "-m", "other friday claimed it")
        git(self.arena, "push", "origin", "main")

        # our clone is stale: it still has the task sitting in queue/
        (self.friday / "bus" / "queue" / "T-20260101-00008.json").write_text(
            json.dumps({"schema": "friday.task/v1", "id": "T-20260101-00008", "action": "echo_ok",
                        "params": {}, "created_at_utc": utc(-60), "created_by": "arena",
                        "lease_seconds": 900}, indent=2), encoding="utf-8")

        proc = run_cli(self.friday, "once")
        self.assertIn("poll: idle", proc.stdout)
        self.assertFalse(self.remote_exists("bus/results/T-20260101-00008.json"),
                         "a task claimed by another Friday must never be executed again")
        self.assertTrue(self.remote_exists("bus/claimed/T-20260101-00008.json"))

    def test_claim_that_cannot_be_pushed_is_aborted(self):
        """If the claim push fails (somebody got there first), abort - run nothing."""
        self.queue_task("T-20260101-00011", "echo_ok")
        sys.path.insert(0, str(BUS_DIR))
        try:
            import importlib
            fb = importlib.import_module("friday_bus")
        finally:
            pass
        cfg = fb.load_config(self.friday)
        bus = fb.Bus(cfg, self.friday, fb.load_actions(self.friday), quiet=True)
        attempts = {"push": 0, "runs": 0}

        def always_fail():
            attempts["push"] += 1
            return False

        def fake_execute(task):
            attempts["runs"] += 1
            return {}

        bus.git.push = always_fail
        bus.execute = fake_execute
        outcome = bus.poll_once()

        self.assertEqual(outcome, "lost-race")
        self.assertEqual(attempts["runs"], 0, "nothing may run when the claim is not published")
        self.assertEqual(attempts["push"], 3, "publish() should retry three times")
        self.assertFalse(self.remote_exists("bus/results/T-20260101-00011.json"))
        self.assertTrue(self.remote_exists("bus/queue/T-20260101-00011.json"),
                        "the task stays queued for whoever holds the real claim")

    def test_stale_claim_is_requeued_and_executed(self):
        expired = {"owner": "friday-2", "host": "DEAD-PC", "pid": 9,
                   "claimed_at_utc": utc(-3600), "lease_expires_utc": utc(-1800)}
        doc = {"schema": "friday.task/v1", "id": "T-20260101-00009", "action": "echo_ok", "params": {},
               "created_at_utc": utc(-3700), "created_by": "arena", "lease_seconds": 900, "claim": expired}
        (self.arena / "bus" / "claimed" / "T-20260101-00009.json").write_text(json.dumps(doc, indent=2),
                                                                            encoding="utf-8")
        git(self.arena, "add", "-A")
        git(self.arena, "commit", "-m", "abandoned claim")
        git(self.arena, "push", "origin", "main")

        proc = run_cli(self.friday, "once")
        self.assertIn("requeued stale claim", proc.stdout)
        result = self.read_remote("bus/results/T-20260101-00009.json")
        self.assertEqual(result["status"], "ok")
        done = self.read_remote("bus/done/T-20260101-00009.json")
        self.assertEqual(done.get("reclaims"), 1)

    def test_read_only_mode_runs_nothing(self):
        config_path = self.friday / "bus" / "bus.config.json"
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        cfg["read_only"] = True
        config_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        self.queue_task("T-20260101-00010", "echo_ok")
        proc = run_cli(self.friday, "once")
        self.assertIn("read-only", proc.stdout)
        self.assertTrue(self.remote_exists("bus/queue/T-20260101-00010.json"),
                        "read_only must leave the task queued")
        self.assertFalse(self.remote_exists("bus/results/T-20260101-00010.json"))

    def test_status_heartbeat_and_question_reach_the_remote(self):
        proc = run_cli(self.friday, "status", "--note", "test heartbeat")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        status = self.read_remote("bus/status/friday.status.json")
        self.assertEqual(status["note"], "test heartbeat")
        self.assertEqual(status["owner"], "friday")
        self.assertEqual(status["friday_version"], "1.0.0")
        self.assertIn("polls", status)

        proc = run_cli(self.friday, "ask", "why does the key get rejected?")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.pull_origin()
        requests = list((self.arena / "bus" / "requests").glob("Q-*.json"))
        self.assertEqual(len(requests), 1)
        asked = json.loads(requests[0].read_text(encoding="utf-8"))
        self.assertIn("why does the key", asked["question"])
        self.assertEqual(asked["schema"], "friday.request/v1")

    def test_local_files_are_never_committed(self):
        """actions.json / bus.config.json / .local must stay local."""
        run_cli(self.friday, "once")
        self.pull_origin()
        self.assertFalse((self.arena / "bus" / "actions.json").exists())
        self.assertFalse((self.arena / "bus" / "bus.config.json").exists())
        self.assertFalse((self.arena / "bus" / ".local").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
