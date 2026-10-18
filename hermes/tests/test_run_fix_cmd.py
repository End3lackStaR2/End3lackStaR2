#!/usr/bin/env python3
"""Static guards for hermes/run-fix.cmd, the double-click entry point.

cmd.exe scripts cannot be executed here, so these tests pin the things that
actually break them on Windows: line endings, encoding, label flow, and an
update sequence that git really accepts.

Run: python -m unittest -v test_run_fix_cmd      (from hermes/tests)
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HERMES = HERE.parent
REPO = HERMES.parent
CMD = HERMES / "run-fix.cmd"
FIX = HERMES / "fix-now.ps1"


class RunFixCmdTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = CMD.read_bytes()
        cls.text = cls.raw.decode("utf-8")

    def test_file_exists_and_is_plain_ascii(self):
        self.assertTrue(CMD.is_file())
        self.assertTrue(all(b < 128 for b in self.raw),
                        "cmd.exe on a Thai locale reads non-ASCII bytes as ANSI and garbles the script")

    def test_uses_crlf_line_endings(self):
        bare_lf = self.raw.count(b"\n") - self.raw.count(b"\r\n")
        self.assertEqual(bare_lf, 0, "LF-only .cmd files break goto labels on some Windows builds")

    def test_has_no_utf8_bom(self):
        self.assertNotEqual(self.raw[:3], b"\xef\xbb\xbf", "a BOM makes cmd.exe echo an error on line 1")

    def test_passes_arguments_through(self):
        self.assertIn("%*", self.text, "-DryRun and friends must reach fix-now.ps1")

    def test_uses_the_bypass_for_double_click(self):
        self.assertIn("-ExecutionPolicy Bypass", self.text)
        self.assertIn("-NoProfile", self.text)
        self.assertIn("-File", self.text)

    def test_branch_and_repo_match_the_real_remote(self):
        branch = re.search(r'set "BRANCH=([^"]+)"', self.text).group(1)
        url = re.search(r'set "REPO_URL=([^"]+)"', self.text).group(1)
        self.assertEqual(branch, "arena/a508b692-end3lackstar2")
        self.assertEqual(url, "https://github.com/End3lackStaR2/End3lackStaR2.git")

        probe = subprocess.run(["git", "ls-remote", "--heads", url, branch],
                               capture_output=True, text=True, timeout=120)
        if probe.returncode != 0:
            self.skipTest(f"cannot reach the remote from here: {probe.stderr[:120]}")
        self.assertIn(branch, probe.stdout, "the branch does not exist on the remote any more")

    def test_never_deletes_or_cleans_the_user_tree(self):
        for danger in ("rd /s", "rmdir /s", "del /f", "git clean", "git reset --hard"):
            self.assertNotIn(danger, self.text.lower(), f"{danger} must never appear in a double-click script")

    def test_never_kills_every_python(self):
        self.assertNotIn("taskkill", self.text.lower())

    def test_waits_for_the_user_to_read_the_verdict(self):
        self.assertIn("pause", self.text.lower())

    def test_all_three_verdicts_are_explained(self):
        self.assertIn("VERDICT: OK", self.text)
        self.assertIn("NO FRESH EVIDENCE", self.text)
        self.assertIn("KEY REJECTED", self.text)

    def test_only_exits_through_known_labels(self):
        labels = set(re.findall(r"^:(\w+)", self.text, re.M))
        gotos = set(re.findall(r"goto\s+:?(\w+)", self.text, re.I))
        self.assertTrue(gotos.issubset(labels), f"goto without target: {sorted(gotos - labels)}")

    def test_target_script_exists_and_has_the_flags_we_pass(self):
        self.assertTrue(FIX.is_file(), "fix-now.ps1 is what this launcher runs")
        body = FIX.read_text(encoding="utf-8-sig")
        self.assertIn("$DryRun", body, "run-fix.cmd forwards -DryRun")
        self.assertIn("$Force", body)

    def test_reports_the_file_it_says_it_writes(self):
        body = FIX.read_text(encoding="utf-8-sig")
        # fix-now.ps1 builds the name by concatenation to survive chat formatting,
        # so match the fragment rather than the finished file name.
        self.assertIn("'fix_now_report'", body)
        self.assertIn("'hermes_'", body)
        self.assertIn("hermes_fix_now_report.txt", self.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FixNowVerdictTest(unittest.TestCase):
    """Regression: a healthy run whose INFO lines went to the captured stderr
    was reported as NO_FRESH_EVIDENCE because the verdict only read the log file."""

    @classmethod
    def setUpClass(cls):
        cls.text = FIX.read_text(encoding="utf-8-sig")

    def test_verdict_reads_captured_stdout_and_stderr(self):
        self.assertIn("$capturedText", self.text)
        self.assertIn("$errFile", self.text.split("$capturedText", 1)[1][:400])
        self.assertIn("$evidence -match 'pusher:error", self.text)

    def test_app_success_line_counts_as_ok(self):
        self.assertIn("Subscribed to chatrooms", self.text)

    def test_key_rejection_is_checked_before_ok(self):
        self.assertLess(self.text.index("KEY_REJECTED' }"), self.text.index("verdict = 'OK'") if "verdict = 'OK'" in self.text else self.text.index("$verdict = 'OK'"))


class FixNowLockedFileTest(unittest.TestCase):
    """Regression: ReadAllText on the child's still-open redirect file threw
    'being used by another process' and the verdict was lost."""

    @classmethod
    def setUpClass(cls):
        cls.text = FIX.read_text(encoding="utf-8-sig")

    def test_captured_files_are_read_with_shared_access(self):
        self.assertNotIn("ReadAllText", self.text)
        self.assertIn("'Read', 'ReadWrite'", self.text)

    def test_locked_file_cannot_break_the_verdict(self):
        self.assertIn("could not read", self.text)
