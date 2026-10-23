#!/usr/bin/env python3
"""Guards the one-command launcher (setup.cmd / setup.ps1).

The launcher finds the python scripts with wildcards, so this test pins the
assumptions it makes. If someone renames or adds a second matching file, the
launcher would pick the wrong one - fail here instead of on the user's machine.

Run: python -m unittest -v test_launcher      (from bridge/tests)
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parent
SETUP_PS1 = BRIDGE / "setup.ps1"
SETUP_CMD = BRIDGE / "setup.cmd"


class LauncherTest(unittest.TestCase):
    def test_exactly_one_file_per_launcher_glob(self):
        self.assertEqual(len(list(BRIDGE.glob("arena*.py"))), 1,
                         "setup.ps1 uses 'arena*.py' - keep it unambiguous")
        self.assertEqual(len(list(BRIDGE.glob("mock*.py"))), 1,
                         "setup.ps1 uses 'mock*.py' - keep it unambiguous")

    def test_extension_folder_is_where_the_launcher_says(self):
        ext = BRIDGE / "extension"
        self.assertTrue((ext / "manifest.json").is_file())
        for name in ("background.js", "content.js", "popup.html", "popup.js"):
            self.assertTrue((ext / name).is_file(), f"missing {name}")

    def test_setup_cmd_calls_setup_ps1_and_stops_on_error(self):
        body = SETUP_CMD.read_text(encoding="utf-8")
        self.assertIn("%~dp0setup.ps1", body, "must work from any working directory")
        self.assertIn("-ExecutionPolicy Bypass", body)
        self.assertIn("if errorlevel 1 pause", body, "double-click users must see the failure")

    def test_setup_ps1_is_plain_ascii(self):
        """PS 5.1 reads a BOM-less file as ANSI; ASCII-only cannot be mangled."""
        raw = SETUP_PS1.read_bytes()
        self.assertTrue(all(b < 128 for b in raw), "keep setup.ps1 ASCII (or add a UTF-8 BOM)")

    def test_jsdom_is_declared_for_the_content_test(self):
        pkg = json.loads((HERE / "package.json").read_text(encoding="utf-8"))
        self.assertIn("jsdom", pkg.get("devDependencies", {}),
                      "test_content_js.mjs needs jsdom, so 'npm install' here must fetch it")
        self.assertEqual(pkg["scripts"]["test"], "node test_content_js.mjs")

    def test_setup_cmd_documents_the_switches_it_forwards(self):
        body = SETUP_CMD.read_text(encoding="utf-8")
        for flag in ("-Ask", "-Stop", "-Port"):
            self.assertIn(flag, body, f"setup.cmd should mention {flag}")
        self.assertIn("%*", body, "switches must reach setup.ps1")

    def test_setup_ps1_avoids_ps7_only_syntax(self):
        src = SETUP_PS1.read_text(encoding="utf-8")
        self.assertNotIn("??", src, "null-coalescing is PS7-only")
        self.assertIsNone(re.search(r"-[A-Za-z]+\s+\(if\b", src), "not a valid argument")
        self.assertIsNone(re.search(r"@\w+\.\w+\(", src), "PS7-only member access")

    def test_setup_ps1_balances_braces(self):
        src = SETUP_PS1.read_text(encoding="utf-8")
        self.assertEqual(src.count("{"), src.count("}"))
        self.assertEqual(src.count("("), src.count(")"))

    def test_token_path_matches_what_the_bridge_writes(self):
        """setup.ps1 tells the user where the token is; arena_bridge.py decides.

        These are two separate files, so nothing but this test keeps them equal.
        """
        bridge = (BRIDGE / "arena_bridge.py").read_text(encoding="utf-8")
        setup = SETUP_PS1.read_text(encoding="utf-8")
        self.assertIn('REPO / "bus" / ".local" / "bridge.token"', bridge,
                      "arena_bridge.py changed its default token location")
        for part in ("'bus'", "'.local'", "'bridge.token'"):
            self.assertIn(part, setup, f"setup.ps1 no longer joins {part}")

    def test_setup_can_prove_the_round_trip(self):
        src = SETUP_PS1.read_text(encoding="utf-8")
        self.assertIn("[switch]$Ask", src)
        self.assertIn("$LASTEXITCODE", src, "must report whether the ask really worked")
        self.assertIn("PROVEN", src, "the user needs an unambiguous success line")

    def test_setup_reuses_a_healthy_bridge_instead_of_failing(self):
        src = SETUP_PS1.read_text(encoding="utf-8")
        self.assertIn("$reused = $false", src)
        # the flag must be decided before it is read
        self.assertLess(src.index("$reused = $false"), src.index("if (-not $reused)"))

    def test_readme_gives_a_command_that_survives_the_script_policy(self):
        readme = (BRIDGE / "README-TH.md").read_text(encoding="utf-8")
        self.assertTrue("-ExecutionPolicy Bypass" in readme or "setup.cmd" in readme,
                        "a bare '.\setup.ps1' is blocked by the default Windows policy")
        self.assertIn("Restricted", readme, "explain why the long form is needed")

    def test_stop_switch_only_targets_our_scripts(self):
        src = SETUP_PS1.read_text(encoding="utf-8")
        self.assertIn("$Stop", src, "needs an opt-in stop switch")
        self.assertNotIn("taskkill", src.lower(), "never kill every python on the machine")
        self.assertIn("CommandLine", src, "must match on the command line, not the image name")


if __name__ == "__main__":
    unittest.main(verbosity=2)
