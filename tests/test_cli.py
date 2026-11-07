#!/usr/bin/env python3
"""The `contextbridge` command line."""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import SandboxCase  # noqa: E402
from contextbridge import __main__ as cli  # noqa: E402


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
