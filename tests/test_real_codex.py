#!/usr/bin/env python3
"""Against the real `codex` CLI, in an isolated CODEX_HOME and user profile.

This pins the importer behaviour ContextBridge depends on, and that helpers.FakeCodexBridge
mirrors. It needs the Codex CLI, makes no model calls and never touches the real ~/.codex.

Run with: CONTEXTBRIDGE_REAL_CODEX=1 python -m unittest tests/test_real_codex.py
"""
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import CLAUDE, CODEX, SandboxCase, converter  # noqa: E402
from contextbridge.codex_bridge import CodexBridge  # noqa: E402
from contextbridge.models import SyncState  # noqa: E402

ENABLED = os.environ.get("CONTEXTBRIDGE_REAL_CODEX") == "1" and shutil.which("codex")


@unittest.skipUnless(ENABLED, "set CONTEXTBRIDGE_REAL_CODEX=1 with the Codex CLI installed")
class TestRealImporter(SandboxCase):
    def setUp(self):
        super().setUp()
        self.real = CodexBridge(self.box.codex_home, user_home=self.box.claude_home.parent)
        self.app.engine.bridge_factory = lambda: self.real_bridge()
        self.app.bridge_factory = self.app.engine.bridge_factory

    def real_bridge(self):
        return CodexBridge(self.box.codex_home, user_home=self.box.claude_home.parent)

    def thread_files(self):
        return list((self.box.codex_home / "sessions").rglob("rollout-*"))

    def test_imports_exactly_one_session(self):
        self.box.claude_session(("an unrelated session", "ignore it"))
        sid = self.box.claude_session(("plan the feature", "planned"), ("build it", "built"))
        result = self.real_bridge().import_claude_session(self.box.find_claude(sid), str(self.box.project))
        self.assertEqual(result.status, "imported")
        self.assertEqual(len(self.thread_files()), 1)
        self.assertEqual(self.box.codex_humans(result.thread_id), ["plan the feature", "build it"])

    def test_unchanged_is_a_no_op_and_growth_appends(self):
        sid = self.box.claude_session(("plan the feature", "planned"))
        path = self.box.find_claude(sid)
        first = self.real_bridge().import_claude_session(path, str(self.box.project))
        same = self.real_bridge().import_claude_session(path, str(self.box.project))
        self.assertEqual(same.status, "no_change")
        self.box.claude_turn(sid, "now build it", "built")
        grown = self.real_bridge().import_claude_session(path, str(self.box.project))
        self.assertEqual(grown.status, "imported")
        self.assertEqual(grown.thread_id, first.thread_id)
        self.assertEqual(len(self.thread_files()), 1)
        self.assertEqual(self.box.codex_humans(first.thread_id), ["plan the feature", "now build it"])

    def test_continued_thread_is_never_written_to(self):
        sid = self.box.claude_session(("plan the feature", "planned"))
        path = self.box.find_claude(sid)
        thread = self.real_bridge().import_claude_session(path, str(self.box.project)).thread_id
        rollout = next((self.box.codex_home / "sessions").rglob(f"*{thread}*"))
        with rollout.open("ab") as fh:
            fh.write(json.dumps(self.box.ev_user("native codex work")).encode() + b"\n")
        before = rollout.read_bytes()
        self.box.claude_turn(sid, "claude moved on", "ok")
        result = self.real_bridge().import_claude_session(path, str(self.box.project))
        self.assertEqual(result.status, "no_change")
        self.assertEqual(rollout.read_bytes(), before)

    def test_paths_outside_claude_projects_are_refused(self):
        outside = self.box.root / "outside" / f"{'a' * 8}-2222-4333-8444-555555555555.jsonl"
        outside.parent.mkdir()
        sid = self.box.claude_session(("plan the feature", "planned"))
        outside.write_bytes(self.box.find_claude(sid).read_bytes())
        result = self.real_bridge().import_claude_session(outside, str(self.box.project))
        self.assertEqual(result.status, "failed")
        self.assertIn("not detected", result.message)

    def test_archive_and_restore(self):
        sid = self.box.claude_session(("plan the feature", "planned"))
        thread = self.real_bridge().import_claude_session(self.box.find_claude(sid), str(self.box.project)).thread_id
        self.real_bridge().archive_thread(thread)
        self.assertTrue(list((self.box.codex_home / "archived_sessions").glob(f"*{thread}*")))
        self.real_bridge().unarchive_thread(thread)
        self.assertFalse(list((self.box.codex_home / "archived_sessions").glob(f"*{thread}*")))

    def test_full_round_trip_with_real_importer_and_real_state_db(self):
        """Codex A -> Claude B -> Codex C -> Claude B, with Codex's own files and database."""
        box = self.box
        a = box.codex_rollout()
        box.codex_turn(a, "A1 start the work", "A1 done", tool=True)
        rollout_a = box.rollout_path(a).read_bytes()
        b = self.send("codex", a, CLAUDE).dest_id
        box.claude_turn(b, "B1 claude continues", "B1 done")
        self.assertEqual(self.get("claude", b).state, SyncState.SOURCE_ADVANCED)
        report = self.send("claude", b, CODEX)
        self.assertEqual(report.status, "committed")
        c = report.dest_id
        humans = box.codex_humans(c)
        self.assertEqual(humans[1:], ["B1 claude continues"])
        self.assertIn("ContextBridge handoff", humans[0])
        self.assertFalse(list(box.claude_home.glob("projects/contextbridge-handoffs/*")))
        rollout_c = next((box.codex_home / "sessions").rglob(f"*{c}*"))
        with rollout_c.open("ab") as fh:
            for row in (box.ev_task_started("n1"), box.ev_user("C1 codex resumes"),
                        box.ev_assistant("C1 done"), box.ev_done("n1")):
                fh.write(json.dumps(row).encode() + b"\n")
        self.assertEqual(self.get("codex", c).state, SyncState.SOURCE_ADVANCED)
        back = self.send("codex", c, CLAUDE)
        self.assertEqual(back.dest_id, b)
        self.assertEqual(box.claude_humans(b), ["A1 start the work", "B1 claude continues", "C1 codex resumes"])
        self.assertEqual(box.rollout_path(a).read_bytes(), rollout_a)
        self.assertEqual(self.get("codex", c).state, SyncState.UNCHANGED)
        self.assertEqual(converter.validate([json.loads(line) for line in box.find_claude(b).read_bytes().splitlines()
                                      if json.loads(line).get("type") in ("user", "assistant")]), [])


if __name__ == "__main__":
    unittest.main()
