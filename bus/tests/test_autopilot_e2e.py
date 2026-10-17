#!/usr/bin/env python3
"""End-to-end tests for autopilot.py against a real git remote.

Proves, without mocks:
  * a workflow runs to 'achieved' when the acceptance criteria are met
  * a failing step retries and then succeeds
  * retries exhausted -> escalation request for Arena + a report, status needs_review
  * a gated action stops the run and asks for a one-time approval (nothing executes)
  * after 'approve', --resume continues and finishes
  * the kill switch (bus/.local/STOP) aborts before anything runs
  * max_total_actions stops a goto loop
  * a repeated failure signature produces an improvement suggestion (once)
  * reports land on the remote under bus/reports/

Run: python -m unittest -v test_autopilot_e2e      (from bus/tests)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUS_DIR = HERE.parent
FRIDAY = BUS_DIR / "friday_bus.py"
AUTOPILOT = BUS_DIR / "autopilot.py"

GIT_ID = ["-c", "user.name=autopilot-test", "-c", "user.email=autopilot-test@localhost"]


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", *GIT_ID, *args], cwd=str(cwd), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {proc.stderr[:400]}")
    return proc


def run(args, cwd):
    proc = subprocess.run([sys.executable, *args], cwd=str(cwd), capture_output=True, text=True)
    return proc


def utc():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AutopilotE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pilot_e2e_"))
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "--bare", "-b", "main", str(self.origin))

        seed = self.tmp / "seed"
        git(self.tmp, "clone", str(self.origin), str(seed))
        bus = seed / "bus"
        bus.mkdir(parents=True)
        for name in ("friday_bus.py", "autopilot.py", "actions.json.example",
                     "policy.json.example", ".gitignore", "PROTOCOL.md"):
            src = BUS_DIR / name
            if src.exists():
                shutil.copy2(src, bus / name)
        for sub in ("queue", "claimed", "results", "done", "status", "requests", "answers",
                    "reports", "suggestions"):
            (bus / sub).mkdir()
            (bus / sub / ".gitkeep").write_text("", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-m", "seed")
        git(seed, "push", "origin", "main")

        self.arena = self.tmp / "arena"
        git(self.tmp, "clone", str(self.origin), str(self.arena))

        self.pilot_dir = self.tmp / "pilot"
        proc = run([str(FRIDAY), "--dir", str(self.pilot_dir), "init",
                    "--repo", str(self.origin), "--branch", "main"], self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        # a counter file lets an action fail once and then succeed
        self.counter = self.pilot_dir / "bus" / ".local" / "counter.txt"
        self.counter.parent.mkdir(parents=True, exist_ok=True)
        self.counter.write_text("0", encoding="utf-8")

        # actions used by the tests
        actions = {
            "schema": "friday.actions/v1",
            "actions": {
                "good": {"program": sys.executable,
                         "args": ["-c", "print('VERDICT: OK')"], "timeout_seconds": 30, "params": {}},
                "bad": {"program": sys.executable,
                        "args": ["-c", "import sys;print('pusher:error code=4001');sys.exit(3)"],
                        "timeout_seconds": 30, "params": {}},
                "flaky": {"program": sys.executable,
                          "args": ["-c",
                                   ("import pathlib,sys;p=pathlib.Path(sys.argv[1]);"
                                    "n=int(p.read_text() or 0)+1;p.write_text(str(n));"
                                    "print('VERDICT: OK' if n>1 else 'not yet');"
                                    "sys.exit(0 if n>1 else 1)"),
                                   "{bus_dir}/.local/counter.txt"],
                          "timeout_seconds": 30, "params": {}},
                "gated": {"program": sys.executable,
                          "args": ["-c", "print('GATED RAN')"], "timeout_seconds": 30, "params": {}},
            },
        }
        (self.pilot_dir / "bus" / "actions.json").write_text(json.dumps(actions, indent=2), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers
    def write_policy(self, workflows, **guardrails):
        policy = {"schema": "friday.policy/v1", "workflows": workflows}
        policy.update(guardrails)
        (self.pilot_dir / "bus" / "policy.json").write_text(json.dumps(policy, indent=2), encoding="utf-8")

    def pilot(self, *args):
        return run([str(AUTOPILOT), "--dir", str(self.pilot_dir), *args], self.tmp)

    def remote_json(self, rel):
        git(self.arena, "fetch", "origin", "main")
        git(self.arena, "reset", "--hard", "origin/main")
        path = self.arena / rel
        self.assertTrue(path.exists(), f"{rel} not on the remote")
        return json.loads(path.read_text(encoding="utf-8"))

    def remote_glob(self, pattern):
        git(self.arena, "fetch", "origin", "main")
        git(self.arena, "reset", "--hard", "origin/main")
        return sorted(self.arena.glob(pattern))

    # -- tests
    def test_happy_path_reaches_achieved(self):
        self.write_policy({"wf": {"steps": [
            {"id": "one", "action": "good", "accept": {"exit_code": 0, "stdout_regex": ["VERDICT:\\s*OK"]}},
        ]}})
        proc = self.pilot("run", "--workflow", "wf")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("status   : achieved", proc.stdout)
        reports = self.remote_glob("bus/reports/R-*.json")
        self.assertEqual(len(reports), 1)
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "achieved")
        self.assertEqual(report["steps"][0]["passed"], True)
        self.assertEqual(report["actions_used"], 1)

    def test_acceptance_regex_can_reject_a_zero_exit(self):
        # 'bad' exits 3, but even exit 0 would be rejected because of the forbidden text
        self.write_policy({"wf": {"steps": [
            {"id": "one", "action": "bad", "accept": {"stdout_absent_regex": ["4001"]},
             "on_fail": "abort"},
        ]}})
        proc = self.pilot("run", "--workflow", "wf")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("status   : aborted", proc.stdout)
        self.assertIn("forbidden", proc.stdout)

    def test_retry_then_success(self):
        self.write_policy({"wf": {"steps": [
            {"id": "one", "action": "flaky", "retries": 1, "accept": {"exit_code": 0}, "on_fail": "retry"},
        ]}})
        proc = self.pilot("run", "--workflow", "wf")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        report = json.loads(self.remote_glob("bus/reports/R-*.json")[0].read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "achieved")
        self.assertEqual(len(report["steps"]), 2, "one failure + one success")
        self.assertEqual(report["steps"][0]["passed"], False)
        self.assertEqual(report["steps"][1]["passed"], True)

    def test_retries_exhausted_escalates_to_arena(self):
        self.write_policy({"wf": {"steps": [
            {"id": "one", "action": "bad", "retries": 1, "accept": {"exit_code": 0}, "on_fail": "retry"},
        ]}})
        proc = self.pilot("run", "--workflow", "wf")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("status   : needs_review", proc.stdout)
        report = json.loads(self.remote_glob("bus/reports/R-*.json")[0].read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "needs_review")
        self.assertTrue(report["escalation_request"].startswith("Q-"))
        questions = self.remote_glob("bus/requests/Q-*.json")
        self.assertEqual(len(questions), 1)
        asked = json.loads(questions[0].read_text(encoding="utf-8"))
        self.assertEqual(asked["kind"], "escalation")
        self.assertIn("autopilot", asked["question"])

    def test_gated_action_waits_then_resumes_after_approval(self):
        self.write_policy({"wf": {"steps": [
            {"id": "g", "action": "gated", "accept": {"exit_code": 0}},
        ]}}, require_approval=["gated"])

        proc = self.pilot("run", "--workflow", "wf")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("status   : awaiting_approval", proc.stdout)
        reports = self.remote_glob("bus/reports/R-*.json")
        self.assertEqual(len(reports), 1, "the awaiting_approval run should be audited")
        gated_report = json.loads(reports[0].read_text(encoding="utf-8"))
        self.assertEqual(gated_report["status"], "awaiting_approval")
        self.assertNotIn("GATED RAN", json.dumps(gated_report))
        approvals = self.remote_glob("bus/requests/Q-*.json")
        self.assertEqual(len(approvals), 1)
        self.assertEqual(json.loads(approvals[0].read_text(encoding="utf-8"))["kind"], "approval")

        proc = self.pilot("approve", "--action", "gated")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        proc = self.pilot("run", "--workflow", "wf", "--resume")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("status   : achieved", proc.stdout)

    def test_kill_switch_aborts_before_running_anything(self):
        self.write_policy({"wf": {"steps": [
            {"id": "one", "action": "good", "accept": {"exit_code": 0}},
        ]}})
        (self.pilot_dir / "bus" / ".local" / "STOP").write_text("stop", encoding="utf-8")
        proc = self.pilot("run", "--workflow", "wf")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("kill switch", proc.stdout)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "0", "no action may run")

    def test_max_total_actions_stops_a_goto_loop(self):
        self.write_policy({"wf": {"steps": [
            {"id": "loop", "action": "good", "accept": {"stdout_regex": ["NEVER-MATCHES"]},
             "on_fail": "goto:loop"},
        ]}}, max_total_actions=3, max_consecutive_failures=99)

        proc = self.pilot("run", "--workflow", "wf")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("max_total_actions (3) reached", proc.stdout)
        report = json.loads(self.remote_glob("bus/reports/R-*.json")[0].read_text(encoding="utf-8"))
        self.assertEqual(report["actions_used"], 3)
        self.assertEqual(len(report["steps"]), 3)

    def test_repeated_failure_produces_one_suggestion(self):
        self.write_policy({"wf": {"steps": [
            {"id": "one", "action": "bad", "accept": {"exit_code": 0}, "on_fail": "abort"},
        ]}}, suggestion_threshold=2)

        self.assertEqual(self.pilot("run", "--workflow", "wf").returncode, 2)
        self.assertEqual(self.remote_glob("bus/suggestions/S-*.json"), [],
                         "one failure is below the threshold")
        self.assertEqual(self.pilot("run", "--workflow", "wf").returncode, 2)

        suggestions = self.remote_glob("bus/suggestions/S-*.json")
        self.assertEqual(len(suggestions), 1, "second occurrence must create exactly one suggestion")
        doc = json.loads(suggestions[0].read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], "friday.suggestion/v1")
        self.assertGreaterEqual(doc["occurrences"], 2)
        self.assertTrue(doc["proposed_options"])

        # a third failure must not duplicate it
        self.pilot("run", "--workflow", "wf")
        self.assertEqual(len(self.remote_glob("bus/suggestions/S-*.json")), 1)

    def test_dry_run_executes_nothing_and_pushes_nothing(self):
        self.write_policy({"wf": {"steps": [
            {"id": "one", "action": "good", "accept": {"exit_code": 0}},
        ]}})
        proc = self.pilot("run", "--workflow", "wf", "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("dry-run", proc.stdout)
        self.assertEqual(self.remote_glob("bus/reports/R-*.json"), [])

    def test_report_summary_reflects_history(self):
        self.write_policy({"wf": {"steps": [
            {"id": "one", "action": "good", "accept": {"exit_code": 0}},
        ]}})
        self.pilot("run", "--workflow", "wf")
        proc = self.pilot("report")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        summary = json.loads(proc.stdout[proc.stdout.index("{"):])
        self.assertEqual(summary["runs"], 1)
        self.assertEqual(summary["achieved"], 1)
        self.assertEqual(summary["success_rate"], 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
