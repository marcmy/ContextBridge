#!/usr/bin/env python3
"""Both command lines over one backend: the original importer and `contextbridge`."""
import contextlib
import io
import json
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import CLAUDE, SandboxCase, ci  # noqa: E402
from contextbridge import __main__ as cli  # noqa: E402


class LegacyCli(SandboxCase):
    def run_cli(self, *argv) -> str:
        out = io.StringIO()
        argv = ["codex_import", "--codex-dir", str(self.box.codex_home / "sessions"),
                "--claude-dir", str(self.box.claude_home / "projects"), *argv]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            ci.main()
        return out.getvalue()

    def test_list_import_status_uninstall_still_work(self):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "still works from the old tool", "yes", tool=True)
        self.assertIn(thread, self.run_cli("list"))
        self.assertIn("would write", self.run_cli("import", "--all", "--dry-run"))
        self.assertFalse(list(self.box.claude_home.glob("projects/*/*.jsonl")))
        self.assertIn("write", self.run_cli("import", "--all", "--mode", "faithful"))
        self.assertEqual(len(list(self.box.claude_home.glob("projects/*/*.jsonl"))), 1)
        self.assertIn("1 import(s) recorded", self.run_cli("status"))
        self.assertIn("Nothing to do", self.run_cli("import", "--all", "--no-sync"))
        self.box.codex_turn(thread, "a newer turn for sync", "yes")
        self.assertIn("appended to", self.run_cli("import", "--all"))
        self.assertIn("All recorded imports", self.run_cli("repair", "--dry-run"))
        self.assertIn("Would remove", self.run_cli("uninstall", "--dry-run"))
        self.assertIn("Removing 1 import", self.run_cli("uninstall"))
        self.assertFalse(list(self.box.claude_home.glob("projects/*/*.jsonl")))

    def test_compressed_rollout_imports_and_stays_current(self):
        try:
            compress = ci._zstd().compress
        except (RuntimeError, AttributeError):
            self.skipTest("no zstd codec")
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "a compressed conversation", "yes")
        self.assertIn("write", self.run_cli("import", "--all"))
        plain = self.box.rollout_path(thread)
        plain.with_name(plain.name + ".zst").write_bytes(compress(plain.read_bytes()))
        plain.unlink()
        out = self.run_cli("import", "--all")
        self.assertIn("already in Claude and up to date", out)
        self.assertNotIn("revision changed", out)
        self.assertIn("healthy", self.run_cli("status"))


class BridgeCli(SandboxCase):
    def run_cli(self, *argv) -> str:
        args = cli.build_parser().parse_args(
            ["--codex-home", str(self.box.codex_home), "--claude-home", str(self.box.claude_home),
             "--app-home", str(self.box.app_home), *argv])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.run_cli(args), 0)
        return out.getvalue()

    def test_sessions_plan_and_send(self):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "from the new command line", "yes")
        rows = json.loads(self.run_cli("sessions", "--json"))
        self.assertEqual(rows[0]["id"], thread)
        self.assertEqual(rows[0]["state"], "not_transferred")
        self.assertIn("new_session", self.run_cli("plan", thread[:13]))
        self.assertFalse(list(self.box.claude_home.glob("projects/*/*.jsonl")))
        self.assertIn("Add --yes", self.run_cli("send", thread[:13]))
        self.assertFalse(list(self.box.claude_home.glob("projects/*/*.jsonl")))
        self.assertIn("committed", self.run_cli("send", thread[:13], "--yes"))
        self.assertEqual(len(list(self.box.claude_home.glob("projects/*/*.jsonl"))), 1)
        self.assertIn("Imported, unchanged", self.run_cli("sessions"))
        self.assertIn("new_session", self.run_cli("transfers"))
        self.assertIn(thread[:13], self.run_cli("show", thread[:13]))


if __name__ == "__main__":
    unittest.main()
