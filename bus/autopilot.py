#!/usr/bin/env python3
"""autopilot.py - Friday decides and acts on its own, inside a policy you approve once.

Honest framing
--------------
Arena (the chat assistant) cannot be called by a machine: it has no API here, no
MCP connector, and it only runs when the human sends a message. So "Friday asks
Arena and continues without the human" is not possible.

What IS possible - and what this file does - is to move the *decision making*
from the human into a policy file:

    policy.json  = what to do, what counts as success, what to do on failure
    autopilot.py = executes it, evaluates results, retries, escalates, reports

The human approves the policy once ("yes, hermes_fix may run"). After that Friday
runs the whole workflow by itself, judges the outcome against explicit acceptance
criteria, retries within limits, and - when it cannot succeed - writes an
escalation question for Arena plus an improvement suggestion, so the next review
is a 10-second read instead of an investigation.

Guardrails (all enforced, not advisory)
---------------------------------------
  * only actions listed in the local bus/actions.json can ever run
  * policy.require_approval gates risky actions until you approve them once
  * max_total_actions / max_consecutive_failures / per-step retries
  * kill switch: create bus/.local/STOP and the next loop iteration aborts
  * cooldown between actions, and a full audit report for every run
  * secrets are redacted (same rules as friday_bus) before anything is pushed

Commands
--------
    python autopilot.py run  --workflow kick_repair          # one full run
    python autopilot.py run  --workflow kick_repair --dry-run
    python autopilot.py approve --action hermes_fix          # one-time approval
    python autopilot.py run  --workflow kick_repair --resume # continue after approval
    python autopilot.py loop --workflow kick_repair --interval 300
    python autopilot.py status
    python autopilot.py report                               # self-evaluation summary
    python autopilot.py selftest

Exit codes: 0 achieved, 1 awaiting approval / needs review, 2 aborted or error.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import friday_bus as fb  # noqa: E402  (same folder)

SCHEMA_POLICY = "friday.policy/v1"
SCHEMA_REPORT = "friday.report/v1"
SCHEMA_SUGGESTION = "friday.suggestion/v1"

DEFAULTS = {
    "schema": SCHEMA_POLICY,
    "max_total_actions": 12,
    "max_consecutive_failures": 3,
    "cooldown_seconds": 0,
    "suggestion_threshold": 2,
    "require_approval": [],
    "workflows": {},
}

FINISH = "__finish__"


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt=None):
    return (dt or utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(text, quiet=False):
    if not quiet:
        print(text, flush=True)


# --------------------------------------------------------------------------- policy

class PolicyError(Exception):
    pass


def load_policy(root: Path, explicit: str = "") -> dict:
    path = Path(explicit) if explicit else (root / "bus" / "policy.json")
    if not path.exists():
        example = root / "bus" / "policy.json.example"
        raise PolicyError(
            f"policy not found: {path}\n"
            f"copy the example first:  copy \"{example}\" \"{path}\"  then edit it")
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise PolicyError(f"policy is not valid JSON: {exc}")
    policy = dict(DEFAULTS)
    policy.update(doc or {})
    if policy.get("schema") != SCHEMA_POLICY:
        raise PolicyError(f"policy.schema must be {SCHEMA_POLICY!r}, got {policy.get('schema')!r}")
    if not isinstance(policy.get("workflows"), dict) or not policy["workflows"]:
        raise PolicyError("policy.workflows must be a non-empty object")
    for name, workflow in policy["workflows"].items():
        steps = workflow.get("steps")
        if not isinstance(steps, list) or not steps:
            raise PolicyError(f"workflow {name!r} has no steps")
        ids = [s.get("id") for s in steps]
        if any(not i for i in ids):
            raise PolicyError(f"workflow {name!r}: every step needs an id")
        if len(set(ids)) != len(ids):
            raise PolicyError(f"workflow {name!r}: duplicate step ids")
        for step in steps:
            if not step.get("action"):
                raise PolicyError(f"workflow {name!r} step {step.get('id')!r}: action is required")
            for key in ("on_pass", "on_fail"):
                value = step.get(key)
                if value and value.startswith("goto:"):
                    target = value.split(":", 1)[1]
                    if target != FINISH and target not in ids:
                        raise PolicyError(f"workflow {name!r} step {step['id']!r}: {key} points to unknown step {target!r}")
            if "retries" in step and not isinstance(step["retries"], int):
                raise PolicyError(f"workflow {name!r} step {step['id']!r}: retries must be an integer")
    return policy


# --------------------------------------------------------------------------- evaluation

def evaluate(result: dict, accept: dict) -> tuple:
    """Judge one action result against the acceptance criteria. Returns (passed, reasons)."""
    reasons = []
    if not accept:
        return True, []

    if "status" in accept:
        allowed = accept["status"]
        allowed = allowed if isinstance(allowed, list) else [allowed]
        if result.get("status") not in allowed:
            reasons.append(f"status {result.get('status')!r} not in {allowed}")

    if "exit_code" in accept:
        allowed = accept["exit_code"]
        allowed = allowed if isinstance(allowed, list) else [allowed]
        if result.get("exit_code") not in allowed:
            reasons.append(f"exit_code {result.get('exit_code')!r} not in {allowed}")

    text = (result.get("stdout_tail") or "") + "\n" + (result.get("stderr_tail") or "")
    for pattern in accept.get("stdout_regex") or []:
        if not re.search(pattern, text, re.MULTILINE):
            reasons.append(f"output does not match /{pattern}/")
    for pattern in accept.get("stdout_absent_regex") or []:
        if re.search(pattern, text, re.MULTILINE):
            reasons.append(f"output contains forbidden /{pattern}/")

    limit = accept.get("max_duration_ms")
    if isinstance(limit, int) and (result.get("duration_ms") or 0) > limit:
        reasons.append(f"took {result.get('duration_ms')} ms (> {limit} ms)")

    return (not reasons), reasons


# --------------------------------------------------------------------------- engine

class Autopilot:
    def __init__(self, bus: fb.Bus, policy: dict, dry_run: bool = False, quiet: bool = False):
        self.bus = bus
        self.policy = policy
        self.dry_run = dry_run
        self.quiet = quiet
        self.local = bus.local
        self.state_path = self.local / "autopilot.state.json"
        self.ledger_path = self.local / "ledger.jsonl"
        self.kill_switch = self.local / "STOP"

    # -- state
    def load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_state(self, state: dict) -> None:
        self.local.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    def clear_state(self) -> None:
        try:
            self.state_path.unlink()
        except OSError:
            pass

    # -- ledger / suggestions
    def ledger_add(self, entry: dict) -> None:
        self.local.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def ledger_read(self) -> list:
        try:
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def maybe_suggest(self, signature: str, detail: dict) -> str:
        """Write an improvement suggestion once a failure signature repeats enough times."""
        threshold = int(self.policy.get("suggestion_threshold") or 2)
        entries = [e for e in self.ledger_read() if e.get("signature") == signature]
        if len(entries) < threshold:
            return ""
        digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:8]
        path = self.bus.path("suggestions", f"S-{digest}.json")
        if path.exists():
            return ""
        doc = {
            "schema": SCHEMA_SUGGESTION,
            "id": f"S-{digest}",
            "created_at_utc": iso(),
            "created_by": "friday-autopilot",
            "signature": signature,
            "occurrences": len(entries),
            "workflow": detail.get("workflow"),
            "step": detail.get("step"),
            "action": detail.get("action"),
            "reasons": detail.get("reasons") or [],
            "recent_evidence": entries[-3:],
            "proposed_options": [
                "loosen or tighten the acceptance criteria if they no longer match reality",
                "add a retry, or a goto: probe step before retrying the failing action",
                "if the failure is 'App key ... not in this cluster', the revoked key is still read somewhere (code, not config)",
                "if the failure is a timeout, raise timeout_seconds in actions.json for that action",
                "if the failure repeats after a real fix, escalate: it needs a human/Arena decision, not another retry",
            ],
            "note": "autopilot never edits policy.json itself - it proposes, a human or Arena decides",
        }
        if self.dry_run:
            log(f"  dry-run: would write suggestion {path.name}", self.quiet)
            return ""
        fb.save_json(path, doc)
        return str(path)

    # -- questions for Arena
    def ask_arena(self, workflow: str, step: str, reasons: list, run_id: str, kind: str = "escalation") -> str:
        question_id = fb.new_id("Q")
        context = "\n".join([
            f"autopilot run {run_id}",
            f"workflow: {workflow}",
            f"step: {step}",
            "reasons: " + ("; ".join(reasons) if reasons else "(none)"),
            "Friday tried everything inside the policy and stopped. The human is not needed to collect data -",
            "the evidence is already in bus/reports/ and bus/suggestions/. A decision is what is missing.",
        ])
        if self.dry_run:
            log(f"  dry-run: would ask Arena (would write {question_id})", self.quiet)
            return question_id
        path = self.bus.path("requests", f"{question_id}.json")
        fb.save_json(path, {
            "schema": fb.SCHEMA_REQUEST,
            "id": question_id,
            "created_at_utc": iso(),
            "created_by": "friday-autopilot",
            "host": self.bus.cfg.get("host"),
            "kind": kind,
            "question": f"[autopilot] {workflow}/{step}: {reasons[0] if reasons else 'step failed'} - what should Friday do next?",
            "context": context,
            "answer_file": f"bus/answers/{question_id}.json",
        })
        return question_id

    # -- one step
    def run_step(self, workflow_name: str, step: dict, run_id: str, attempt: int) -> dict:
        action = step["action"]
        params = step.get("params") or {}
        task = {
            "schema": fb.SCHEMA_TASK,
            "id": f"{run_id}-{step['id']}-a{attempt}",
            "created_at_utc": iso(),
            "created_by": "friday-autopilot",
            "action": action,
            "params": params,
            "lease_seconds": 900,
            "notes": f"autopilot {workflow_name}/{step['id']} attempt {attempt}",
        }
        if self.dry_run:
            spec = self.bus.actions.get(action)
            log(f"  dry-run: {step['id']} -> {action} (allowlisted={'yes' if spec else 'NO'})", self.quiet)
            return {"id": task["id"], "status": "dry-run", "exit_code": None, "duration_ms": 0,
                    "stdout_tail": "", "stderr_tail": "", "action": action}
        log(f"  run: {step['id']:12s} -> {action} (attempt {attempt})", self.quiet)
        result = self.bus.execute(task)
        log(f"       {result.get('status')} exit={result.get('exit_code')} {result.get('duration_ms')} ms",
            self.quiet)
        return result

    # -- main
    def run(self, workflow_name: str, resume: bool = False) -> dict:
        workflows = self.policy["workflows"]
        if workflow_name not in workflows:
            raise PolicyError(f"unknown workflow {workflow_name!r} (have: {', '.join(sorted(workflows))})")
        workflow = workflows[workflow_name]
        steps = workflow["steps"]
        ids = [s["id"] for s in steps]

        if resume:
            state = self.load_state()
            if not state or state.get("workflow") != workflow_name:
                log("  no saved state for this workflow - starting fresh", self.quiet)
                state = {}
        else:
            state = {}

        if not state:
            state = {
                "run_id": fb.new_id("R"),
                "workflow": workflow_name,
                "started_at_utc": iso(),
                "index": 0,
                "attempts": 0,
                "actions_used": 0,
                "consecutive_failures": 0,
                "approved": {},
                "history": [],
            }

        run_id = state["run_id"]
        max_actions = int(self.policy.get("max_total_actions") or DEFAULTS["max_total_actions"])
        max_failures = int(self.policy.get("max_consecutive_failures") or DEFAULTS["max_consecutive_failures"])
        cooldown = float(self.policy.get("cooldown_seconds") or 0)
        require_approval = list(self.policy.get("require_approval") or [])

        status = "running"
        reasons: list = []
        escalation = ""
        while True:
            if self.kill_switch.exists():
                status, reasons = "aborted", ["kill switch: bus/.local/STOP exists"]
                break
            if state["index"] >= len(steps):
                status = "achieved"
                break
            if state["actions_used"] >= max_actions:
                status, reasons = "aborted", [f"max_total_actions ({max_actions}) reached"]
                break

            step = steps[state["index"]]
            action = step["action"]

            if action in require_approval and not state["approved"].get(action):
                if self.dry_run:
                    log(f"  dry-run: would stop and ask approval for {action}", self.quiet)
                    status = "awaiting_approval"
                    reasons = [f"{action} needs one-time approval"]
                    break
                question = self.ask_arena(workflow_name, step["id"],
                                          [f"{action} requires approval before it may run"], run_id,
                                          kind="approval")
                state["pending_approval"] = {"action": action, "request": question}
                status = "awaiting_approval"
                reasons = [f"{action} needs one-time approval (asked Arena in {question})"]
                break

            attempt = state["attempts"] + 1
            result = self.run_step(workflow_name, step, run_id, attempt)
            if not self.dry_run:
                state["actions_used"] += 1
                state["attempts"] = attempt
            passed, step_reasons = evaluate(result, step.get("accept") or {})

            state["history"].append({
                "step": step["id"],
                "action": action,
                "attempt": attempt,
                "passed": passed,
                "reasons": step_reasons,
                "status": result.get("status"),
                "exit_code": result.get("exit_code"),
                "duration_ms": result.get("duration_ms"),
            })

            if self.dry_run:
                status = "dry-run"
                break

            if passed:
                state["attempts"] = 0
                state["consecutive_failures"] = 0
                nxt = step.get("on_pass") or "next"
                if nxt == "next":
                    state["index"] += 1
                elif nxt.startswith("goto:"):
                    target = nxt.split(":", 1)[1]
                    state["index"] = len(steps) if target == FINISH else ids.index(target)
                elif nxt in ("finish", FINISH):
                    state["index"] = len(steps)
                else:
                    raise PolicyError(f"step {step['id']!r}: unsupported on_pass {nxt!r}")
                self.save_state(state)
                if cooldown:
                    time.sleep(cooldown)
                continue

            # failure
            state["consecutive_failures"] += 1
            signature = f"{workflow_name}:{step['id']}:{action}:{(step_reasons[0] if step_reasons else result.get('status'))[:80]}"
            self.ledger_add({
                "ts": iso(), "run_id": run_id, "workflow": workflow_name, "step": step["id"],
                "action": action, "attempt": attempt, "signature": signature,
                "status": result.get("status"), "reasons": step_reasons,
            })
            self.maybe_suggest(signature, {"workflow": workflow_name, "step": step["id"],
                                           "action": action, "reasons": step_reasons})

            if state["consecutive_failures"] >= max_failures:
                status, reasons = "aborted", [f"max_consecutive_failures ({max_failures}) reached"] + step_reasons
                break

            policy_fail = (step.get("on_fail") or "escalate").strip()
            retries = int(step.get("retries") or 0)
            if policy_fail == "retry":
                if attempt <= retries:
                    self.save_state(state)
                    if cooldown:
                        time.sleep(cooldown)
                    continue
                status, reasons = "needs_review", step_reasons + [f"retries ({retries}) exhausted"]
                escalation = self.ask_arena(workflow_name, step["id"], reasons, run_id)
                break
            if policy_fail.startswith("goto:"):
                target = policy_fail.split(":", 1)[1]
                state["attempts"] = 0
                state["index"] = len(steps) if target == FINISH else ids.index(target)
                self.save_state(state)
                continue
            if policy_fail == "next":
                state["attempts"] = 0
                state["index"] += 1
                self.save_state(state)
                continue
            if policy_fail == "abort":
                status, reasons = "aborted", step_reasons
                break
            # default: escalate
            status, reasons = "needs_review", step_reasons
            escalation = self.ask_arena(workflow_name, step["id"], reasons, run_id)
            break

        # persist before reporting so 'approve' + 'run --resume' can continue
        # exactly where this run stopped (awaiting_approval / needs_review)
        if not self.dry_run:
            self.save_state(state)
        state["finished_at_utc"] = iso()
        report = self.build_report(state, status, reasons, escalation, workflow_name)
        self.finish(report)
        return report

    # -- outputs
    def build_report(self, state, status, reasons, escalation, workflow_name) -> dict:
        steps_out = []
        for item in state.get("history") or []:
            steps_out.append({
                "step": item.get("step"),
                "action": item.get("action"),
                "attempt": item.get("attempt"),
                "passed": item.get("passed"),
                "reasons": item.get("reasons"),
                "status": item.get("status"),
                "exit_code": item.get("exit_code"),
                "duration_ms": item.get("duration_ms"),
            })
        return {
            "schema": SCHEMA_REPORT,
            "run_id": state.get("run_id"),
            "workflow": workflow_name,
            "status": status,
            "started_at_utc": state.get("started_at_utc"),
            "finished_at_utc": state.get("finished_at_utc"),
            "actions_used": state.get("actions_used"),
            "steps": steps_out,
            "reasons": reasons,
            "escalation_request": escalation,
            "host": self.bus.cfg.get("host"),
            "dry_run": self.dry_run,
            "policy_require_approval": self.policy.get("require_approval"),
        }

    def finish(self, report: dict) -> None:
        if self.dry_run:
            log(json.dumps(report, ensure_ascii=False, indent=2), self.quiet)
            log("  dry-run: nothing written, nothing pushed", self.quiet)
            return
        path = self.bus.path("reports", f"{report['run_id']}.json")
        fb.save_json(path, report)
        ok = self.bus.publish(f"autopilot: {report['workflow']} {report['status']} ({report['run_id']})")
        if report["status"] == "achieved":
            self.clear_state()
        else:
            state = self.load_state()
            state["last_report"] = str(path)
            state["last_status"] = report["status"]
            self.save_state(state)
        log(f"  report: {path}   pushed={'yes' if ok else 'NO (will retry next run)'}", self.quiet)

    # -- self evaluation
    def summary(self) -> dict:
        entries = self.ledger_read()
        total = len(entries)
        by_action = {}
        by_signature = {}
        for entry in entries:
            action = entry.get("action") or "?"
            by_action.setdefault(action, {"attempts": 0, "failures": 0})
            by_action[action]["attempts"] += 1
            by_action[action]["failures"] += 1
            signature = entry.get("signature") or "?"
            by_signature[signature] = by_signature.get(signature, 0) + 1
        reports = []
        for path in sorted(self.bus.path("reports").glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            reports.append({"run_id": doc.get("run_id"), "workflow": doc.get("workflow"),
                            "status": doc.get("status"), "finished": doc.get("finished_at_utc"),
                            "actions_used": doc.get("actions_used")})
        achieved = sum(1 for r in reports if r["status"] == "achieved")
        return {
            "runs": len(reports),
            "achieved": achieved,
            "success_rate": (round(100.0 * achieved / len(reports), 1) if reports else None),
            "failed_attempts_logged": total,
            "failures_by_action": by_action,
            "top_failure_signatures": sorted(by_signature.items(), key=lambda kv: -kv[1])[:5],
            "recent_reports": reports[-5:],
            "suggestions": [p.name for p in sorted(self.bus.path("suggestions").glob("*.json"))],
        }


# --------------------------------------------------------------------------- commands

def make_autopilot(args) -> Autopilot:
    root = Path(args.dir).expanduser().resolve()
    cfg = fb.load_config(root, args.config)
    actions = fb.load_actions(root)
    if not actions:
        raise SystemExit(f"no actions loaded from {root / 'bus' / 'actions.json'} - copy the example first")
    policy = load_policy(root, args.policy)
    bus = fb.Bus(cfg, root, actions, quiet=args.quiet)
    # status/report/selftest have no --dry-run flag
    return Autopilot(bus, policy, dry_run=bool(getattr(args, "dry_run", False)), quiet=args.quiet)


def cmd_run(args) -> int:
    pilot = make_autopilot(args)
    report = pilot.run(args.workflow, resume=args.resume)
    status = report.get("status")
    if not args.quiet:
        print()
        print(f"  workflow : {report.get('workflow')}")
        print(f"  status   : {status}")
        if report.get("reasons"):
            for reason in report["reasons"]:
                print(f"  reason   : {reason}")
        if report.get("escalation_request"):
            print(f"  asked    : {report['escalation_request']}")
    return {"achieved": 0, "dry-run": 0, "awaiting_approval": 1, "needs_review": 1}.get(status, 2)


def cmd_loop(args) -> int:
    pilot = make_autopilot(args)
    interval = int(args.interval or 300)
    log(f"autopilot loop: {args.workflow} every {interval}s (Ctrl+C stops, bus/.local/STOP aborts a run)")
    try:
        while True:
            report = pilot.run(args.workflow, resume=args.resume)
            log(f"  {iso()} -> {report.get('status')}")
            if report.get("status") == "achieved":
                log("  goal achieved - stopping the loop")
                return 0
            if pilot.kill_switch.exists():
                log("  kill switch present - stopping the loop")
                return 2
            time.sleep(interval)
    except KeyboardInterrupt:
        log("\nstopped")
        return 0


def cmd_approve(args) -> int:
    root = Path(args.dir).expanduser().resolve()
    pilot = make_autopilot(args)
    state = pilot.load_state()
    if not state:
        print("no saved run - approve applies to a run that is waiting for approval")
        return 1
    state.setdefault("approved", {})[args.action] = True
    state.pop("pending_approval", None)
    pilot.save_state(state)
    print(f"approved once: {args.action} (run {state.get('run_id')})")
    print("next: python autopilot.py run --workflow " + str(state.get("workflow")) + " --resume")
    return 0


def cmd_status(args) -> int:
    pilot = make_autopilot(args)
    state = pilot.load_state()
    print(json.dumps({"state": state, "kill_switch": pilot.kill_switch.exists(),
                      "policy_workflows": sorted(pilot.policy["workflows"])},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_report(args) -> int:
    pilot = make_autopilot(args)
    print(json.dumps(pilot.summary(), ensure_ascii=False, indent=2))
    return 0


def cmd_selftest(args) -> int:
    passes, fails = 0, []

    def check(name, cond, detail=""):
        nonlocal passes
        if cond:
            passes += 1
            print(f"  [PASS] {name}")
        else:
            fails.append(f"{name} {detail}")
            print(f"  [FAIL] {name} {detail}")

    ok_result = {"status": "ok", "exit_code": 0, "duration_ms": 1000,
                 "stdout_tail": "VERDICT: OK\nconnected to pusher", "stderr_tail": ""}
    passed, reasons = evaluate(ok_result, {"exit_code": 0, "stdout_regex": [r"VERDICT:\s*OK"]})
    check("accepts a good result", passed and not reasons, str(reasons))

    passed, reasons = evaluate(ok_result, {"stdout_regex": [r"VERDICT:\s*OK"], "stdout_absent_regex": ["4001"]})
    check("accepts when forbidden text is absent", passed, str(reasons))

    bad = dict(ok_result, stdout_tail="pusher:error code=4001 App key not in this cluster")
    passed, reasons = evaluate(bad, {"stdout_absent_regex": ["4001"]})
    check("rejects forbidden text", not passed and reasons)

    passed, reasons = evaluate(dict(ok_result, status="timeout"), {"status": "ok"})
    check("rejects wrong status", not passed, str(reasons))

    passed, reasons = evaluate(ok_result, {"max_duration_ms": 500})
    check("rejects too slow", not passed, str(reasons))

    passed, _ = evaluate(ok_result, {"exit_code": [0, 1]})
    check("accepts a list of exit codes", passed)

    passed, _ = evaluate(ok_result, {})
    check("no criteria means pass", passed)

    # policy validation
    try:
        import tempfile
        tmp = Path(tempfile.mkdtemp(prefix="policy_"))
        (tmp / "bus").mkdir()
        (tmp / "bus" / "policy.json").write_text(json.dumps({
            "schema": SCHEMA_POLICY,
            "workflows": {"x": {"steps": [{"id": "a", "action": "echo", "on_fail": "goto:nope"}]}},
        }), encoding="utf-8")
        load_policy(tmp)
        check("rejects a goto to an unknown step", False, "no error raised")
    except PolicyError:
        check("rejects a goto to an unknown step", True)
    except Exception as exc:  # noqa: BLE001
        check("rejects a goto to an unknown step", False, str(exc))

    print()
    print(f"self test: {passes} passed, {len(fails)} failed")
    for item in fails:
        print("  -", item)
    return 2 if fails else 0


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dir", default=".", help="bus clone directory")
    common.add_argument("--config", default="")
    common.add_argument("--policy", default="")
    common.add_argument("--quiet", action="store_true")

    # Same flags after the verb too, with SUPPRESS so they never overwrite values
    # given before it ("--dir X run" and "run --dir X" must both work).
    after = argparse.ArgumentParser(add_help=False)
    after.add_argument("--dir", default=argparse.SUPPRESS)
    after.add_argument("--config", default=argparse.SUPPRESS)
    after.add_argument("--policy", default=argparse.SUPPRESS)
    after.add_argument("--quiet", action="store_true", default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(description="Friday autopilot: decide and act inside a policy.",
                                     parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", parents=[after], help="run a workflow once")
    p.add_argument("--workflow", required=True)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("loop", parents=[after], help="run a workflow repeatedly until achieved")
    p.add_argument("--workflow", required=True)
    p.add_argument("--interval", type=int, default=0)
    p.add_argument("--resume", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_loop)

    p = sub.add_parser("approve", parents=[after], help="approve one gated action for the saved run")
    p.add_argument("--action", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("status", parents=[after], help="show saved state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("report", parents=[after], help="self-evaluation summary")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("selftest", parents=[after], help="offline checks")
    p.set_defaults(func=cmd_selftest)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PolicyError as exc:
        print(f"policy error: {exc}")
        return 2
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
