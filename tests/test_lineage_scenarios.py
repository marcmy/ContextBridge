#!/usr/bin/env python3
"""The acceptance scenarios: synthetic homes, the real conversion and lineage code."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import CLAUDE, CODEX, Sandbox, SandboxCase, converter  # noqa: E402
from contextbridge import handoff as handoff_module  # noqa: E402
from contextbridge.handoff import HandoffError, HandoffOptions  # noqa: E402
from contextbridge.models import SyncState  # noqa: E402
from contextbridge.service import CleanupRefused, resume_command, resume_text  # noqa: E402
from contextbridge.workspace import inspect_workspace  # noqa: E402


class TestDiscovery(SandboxCase):
    def thread(self, *turns):
        thread = self.box.codex_rollout()
        for human, answer in turns:
            self.box.codex_turn(thread, human, answer)
        return thread

    def test_rollout_revisions_are_one_thread(self):
        thread = self.thread(("first request here", "first answer"))
        first = self.box.rollout_path(thread)
        rollout_a = thread  # the first file is named by the thread id alone
        end = len(first.read_bytes())
        revision = "01a1ffff-0000-7000-8000-000000000001"
        self.box.codex_rollout(thread, rollout=revision,
                               base={"thread_id": rollout_a, "end_byte_offset": end,
                                     "end_ordinal_exclusive": 3})
        self.box.codex_turn(thread, "second request here", "second answer")
        snap = self.refresh()
        matching = [s for s in snap.sessions if s.session_id == thread]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0].revisions, 2)
        self.assertEqual(matching[0].human_turns, 2)

    def test_internal_agents_are_hidden_but_known(self):
        visible = self.thread(("a real conversation", "yes"))
        guardian = self.box.codex_rollout(source={"subagent": {"other": "guardian"}})
        scan = self.box.codex_rollout(source="exec", thread_source="security_scan")
        spawned = self.box.codex_rollout(
            source={"subagent": {"thread_spawn": {"parent_thread_id": visible}}})
        snap = self.refresh()
        listed = {s.session_id for s in snap.sessions}
        self.assertIn(visible, listed)
        self.assertFalse(listed & {guardian, scan, spawned})
        self.assertIn(("codex", guardian), snap.by_key)
        everything = self.app.refresh(include_internal=True)
        self.assertTrue({guardian, scan, spawned} <= {s.session_id for s in everything.sessions})

    def test_state_db_is_authoritative_for_title_and_archive(self):
        thread = self.thread(("work on the thing", "ok"))
        db = sqlite3.connect(self.box.codex_home / "state_5.sqlite")
        db.execute("CREATE TABLE threads (id TEXT, rollout_path TEXT, created_at INTEGER, "
                   "updated_at INTEGER, source TEXT, thread_source TEXT, cwd TEXT, title TEXT, "
                   "name TEXT, archived INTEGER, git_branch TEXT, tokens_used INTEGER, model TEXT, "
                   "originator TEXT, first_user_message TEXT, created_at_ms INTEGER, updated_at_ms INTEGER)")
        db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (thread, "\\\\?\\" + str(self.box.rollout_path(thread)), 1, 2, "vscode", "user",
                    "\\\\?\\" + str(self.box.project), "raw", "Named in Codex", 1, "feature/x", 42,
                    "gpt", "Codex Desktop", "first", 1000, 2000))
        db.commit()
        db.close()
        s = self.get("codex", thread)
        self.assertEqual(s.title, "Named in Codex")
        self.assertTrue(s.archived)
        self.assertEqual(s.cwd, str(self.box.project))
        self.assertEqual(s.git_branch, "feature/x")

    def test_compressed_rollouts_read_like_plain_ones(self):
        try:
            zstd = converter._zstd()
            compress = zstd.compress
        except (RuntimeError, AttributeError):
            self.skipTest("no zstd codec")
        thread = self.thread(("compressed request text", "compressed answer"))
        plain = self.box.rollout_path(thread)
        packed = plain.with_name(plain.name + ".zst")
        packed.write_bytes(compress(plain.read_bytes()))
        plain.unlink()
        s = self.get("codex", thread)
        self.assertEqual(s.path, packed)
        self.assertEqual(s.human_turns, 1)
        report = self.send("codex", thread, CLAUDE)
        self.assertEqual(report.status, "committed")
        self.assertIn("compressed request text", self.box.claude_humans(report.dest_id))
        self.assertEqual(self.get("codex", thread).state, SyncState.UNCHANGED)

    def test_identical_titles_and_paths_are_separate_conversations(self):
        a = self.thread(("Fix the bug", "fixed one"))
        b = self.thread(("Fix the bug", "fixed two"))
        c = self.box.claude_session(("Fix the bug", "fixed three"))
        snap = self.refresh()
        ids = {snap.by_key[("codex", a)].conversation_id, snap.by_key[("codex", b)].conversation_id,
               snap.by_key[("claude", c)].conversation_id}
        self.assertEqual(len(ids), 3)
        self.assertFalse(snap.duplicates)  # fewer than 3 turns of overlap is not even "possible"

    def test_windows_paths_are_normalized(self):
        self.assertEqual(converter.normalize_path("\\\\?\\C:\\Users\\m\\x.jsonl"), "C:\\Users\\m\\x.jsonl")
        self.assertEqual(converter.normalize_path("\\\\?\\UNC\\srv\\s\\x"), "\\\\srv\\s\\x")
        self.assertEqual(converter.slug_for("C:\\Users\\m\\src\\Repo"), "C--Users-m-src-Repo")
        self.assertEqual(converter.group_scratch("C:\\Users\\m\\Documents\\Codex\\2026-09-13\\some-slug"),
                         "C:\\Users\\m\\Documents\\Codex")
        self.assertEqual(converter.group_scratch("C:\\Users\\m\\src\\Repo"), "C:\\Users\\m\\src\\Repo")
        self.assertTrue(converter.same_rollout("\\\\?\\C:\\a\\r.jsonl", "C:\\a\\r.jsonl.zst"))


class TestCodexToClaude(SandboxCase):
    def setUp(self):
        super().setUp()
        self.thread = self.box.codex_rollout()
        self.box.codex_turn(self.thread, "implement the parser", "parser done", tool=True)
        self.box.codex_turn(self.thread, "add the tests", "tests added")

    def test_preview_writes_nothing(self):
        session = self.get("codex", self.thread)
        before = _tree(self.box.claude_home), _tree(self.box.codex_home)
        with patch.object(converter, "claude_summary", side_effect=AssertionError("billable call")):
            plan = self.app.plan(session, CLAUDE, HandoffOptions(summary_mode="claude",
                                                                 context_budget=20000))
        self.assertEqual(plan.strategy, "new_session")
        self.assertGreater(plan.estimated_after, 0)
        self.assertEqual((_tree(self.box.claude_home), _tree(self.box.codex_home)), before)

    def test_new_session_then_same_again_does_not_duplicate(self):
        report = self.send("codex", self.thread, CLAUDE)
        self.assertEqual(report.status, "committed")
        self.assertEqual(self.box.claude_humans(report.dest_id),
                         ["implement the parser", "add the tests"])
        self.assertTrue(Path(report.report_path).is_file())

        session = self.get("codex", self.thread)
        self.assertEqual(session.state, SyncState.UNCHANGED)
        plan = self.app.plan(session, CLAUDE, self.options())
        self.assertEqual(plan.strategy, "none")
        self.assertFalse(plan.can_execute)
        self.assertEqual(len(list(self.box.claude_home.glob("projects/*/*.jsonl"))), 1)

    def test_source_only_advance_appends_only_new_turns(self):
        report = self.send("codex", self.thread, CLAUDE)
        self.box.codex_turn(self.thread, "fix the regression", "regression fixed", tool=True)
        session = self.get("codex", self.thread)
        self.assertEqual(session.state, SyncState.SOURCE_ADVANCED)
        again = self.send("codex", self.thread, CLAUDE)
        self.assertEqual(again.strategy, "append")
        self.assertEqual(again.dest_id, report.dest_id)
        self.assertEqual(self.box.claude_humans(report.dest_id),
                         ["implement the parser", "add the tests", "fix the regression"])
        self.assertTrue(again.backups and Path(again.backups[0]).is_file())
        self.assertEqual(self.get("codex", self.thread).state, SyncState.UNCHANGED)
        # Resending right away adds nothing.
        self.assertFalse(self.app.plan(self.get("codex", self.thread), CLAUDE, self.options()).can_execute)

    def test_destination_only_advance_preserves_claude_work(self):
        report = self.send("codex", self.thread, CLAUDE)
        self.box.claude_turn(report.dest_id, "continue in claude", "claude answer")
        session = self.get("codex", self.thread)
        self.assertEqual(session.state, SyncState.DEST_ADVANCED)
        plan = self.app.plan(session, CLAUDE, self.options())
        self.assertFalse(plan.can_execute)
        self.assertEqual(self.get("claude", report.dest_id).state, SyncState.SOURCE_ADVANCED)
        self.assertFalse(self.app.can_archive(self.get("claude", report.dest_id))[0])

    def test_both_advanced_does_not_merge_by_default(self):
        report = self.send("codex", self.thread, CLAUDE)
        original = self.box.find_claude(report.dest_id)
        self.box.claude_turn(report.dest_id, "claude branch work", "claude answer")
        self.box.codex_turn(self.thread, "codex branch work", "codex answer")
        snapshot = original.read_bytes()

        session = self.get("codex", self.thread)
        self.assertEqual(session.state, SyncState.BOTH_ADVANCED)
        plan = self.app.plan(session, CLAUDE, self.options())
        self.assertEqual(plan.strategy, "continuation")
        self.assertFalse(plan.option("append").enabled)
        with self.assertRaises(HandoffError):
            self.app.plan(session, CLAUDE, self.options(), "append")

        result = self.app.execute(plan)
        self.assertNotEqual(result.dest_id, report.dest_id)
        self.assertEqual(original.read_bytes(), snapshot)
        branch = self.box.claude_humans(result.dest_id)
        self.assertIn("codex branch work", branch)
        self.assertNotIn("claude branch work", branch)
        self.assertEqual(self.get("claude", result.dest_id).conversation_id,
                         self.get("claude", report.dest_id).conversation_id)

    def test_missing_destination_offers_new_session(self):
        report = self.send("codex", self.thread, CLAUDE)
        self.box.find_claude(report.dest_id).unlink()
        session = self.get("codex", self.thread)
        self.assertEqual(session.state, SyncState.MISSING_DEST)
        again = self.app.execute(self.app.plan(session, CLAUDE, self.options()))
        self.assertEqual(again.strategy, "new_session")
        self.assertIn("implement the parser", self.box.claude_humans(again.dest_id))

    def test_modified_source_blocks_append(self):
        self.send("codex", self.thread, CLAUDE)
        path = self.box.rollout_path(self.thread)
        path.write_bytes(path.read_bytes().replace(b"implement the parser", b"implement the PARSER"))
        session = self.get("codex", self.thread)
        self.assertEqual(session.state, SyncState.MODIFIED_SOURCE)
        plan = self.app.plan(session, CLAUDE, self.options())
        self.assertEqual(plan.strategy, "continuation")
        self.assertFalse(plan.option("append").enabled)

    def test_revert_that_removes_transferred_turns_is_not_appended(self):
        first = self.box.rollout_path(self.thread)
        keep = _offset_after_first_turn(first)
        self.send("codex", self.thread, CLAUDE)
        revision = "01a1ffff-0000-7000-8000-000000000002"
        self.box.codex_rollout(self.thread, rollout=revision,
                               base={"thread_id": self.thread, "end_byte_offset": keep,
                                     "end_ordinal_exclusive": 3})
        self.box.codex_turn(self.thread, "a different direction", "ok")
        session = self.get("codex", self.thread)
        self.assertEqual(session.state, SyncState.MODIFIED_SOURCE)
        self.assertIn("revert", session.state_detail)

    def test_revision_that_keeps_transferred_turns_is_a_clean_append(self):
        self.send("codex", self.thread, CLAUDE)
        first = self.box.rollout_path(self.thread)
        end = len(first.read_bytes())
        revision = "01a1ffff-0000-7000-8000-000000000003"
        self.box.codex_rollout(self.thread, rollout=revision,
                               base={"thread_id": self.thread, "end_byte_offset": end,
                                     "end_ordinal_exclusive": 9})
        self.box.codex_turn(self.thread, "after the revision", "ok")
        self.assertEqual(self.get("codex", self.thread).state, SyncState.SOURCE_ADVANCED)
        report = self.send("codex", self.thread, CLAUDE)
        self.assertEqual(self.box.claude_humans(report.dest_id),
                         ["implement the parser", "add the tests", "after the revision"])

    def test_stale_preview_is_refused(self):
        report = self.send("codex", self.thread, CLAUDE)
        self.box.codex_turn(self.thread, "newer work", "ok")
        plan = self.app.plan(self.get("codex", self.thread), CLAUDE, self.options())
        self.box.claude_turn(report.dest_id, "while you were reading the preview", "hm")
        before = self.box.find_claude(report.dest_id).read_bytes()
        with self.assertRaisesRegex(HandoffError, "changed since this preview"):
            self.app.execute(plan)
        self.assertEqual(self.box.find_claude(report.dest_id).read_bytes(), before)

    def test_cancel_before_write_leaves_nothing(self):
        plan = self.app.plan(self.get("codex", self.thread), CLAUDE, self.options())
        cancel = threading.Event()
        cancel.set()
        before = _tree(self.box.claude_home)
        report = self.app.execute(plan, cancel=cancel)
        self.assertEqual(report.status, "cancelled")
        self.assertEqual(_tree(self.box.claude_home), before)

    def test_failed_validation_restores_the_original(self):
        report = self.send("codex", self.thread, CLAUDE)
        self.box.codex_turn(self.thread, "new work", "ok")
        plan = self.app.plan(self.get("codex", self.thread), CLAUDE, self.options())
        path = self.box.find_claude(report.dest_id)
        before = path.read_bytes()
        with patch.object(handoff_module.HandoffEngine, "_validate_written",
                          return_value=["simulated corruption"]):
            with self.assertRaisesRegex(HandoffError, "failed validation"):
                self.app.execute(plan)
        self.assertEqual(path.read_bytes(), before)
        # The checkpoint didn't move, so the work is still pending.
        self.assertEqual(self.get("codex", self.thread).state, SyncState.SOURCE_ADVANCED)
        transfers = self.app.store.transfers()
        self.assertEqual(transfers[0]["status"], "failed")
        backups = list((self.box.claude_home / "codex-import-backups").glob("*.jsonl"))
        self.assertTrue(backups)

    def test_crash_during_write_is_flagged_on_next_start(self):
        plan = self.app.plan(self.get("codex", self.thread), CLAUDE, self.options())
        with patch.object(converter, "write_import", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.app.execute(plan)
        snap = self.refresh()
        self.assertTrue(snap.problems and "interrupted" in snap.problems[0])
        self.assertEqual(self.app.store.transfers()[0]["status"], "interrupted")

    def test_active_destination_is_never_appended_to(self):
        report = self.send("codex", self.thread, CLAUDE)
        self.box.codex_turn(self.thread, "new work", "ok")
        live = self.box.claude_home / "sessions"
        live.mkdir()
        (live / f"{os.getpid()}.json").write_text(
            json.dumps({"pid": os.getpid(), "sessionId": report.dest_id}), encoding="utf-8")
        plan = self.app.plan(self.get("codex", self.thread), CLAUDE, self.options())
        self.assertIn("in use", plan.blocked)
        self.assertFalse(plan.can_execute)

    def test_ai_summary_needs_authorization_and_counts_calls(self):
        for i in range(40):
            self.box.codex_turn(self.thread, f"request number {i} with detail", "done " + "x" * 3000)
        session = self.get("codex", self.thread)
        opts = HandoffOptions(context_budget=9000, summary_mode="claude", summary_tokens=1500,
                              max_tool_chars=400)
        plan = self.app.plan(session, CLAUDE, opts)
        self.assertTrue(any("authorization" in w for w in plan.warnings))
        with self.assertRaisesRegex(HandoffError, "not authorized"):
            self.app.execute(plan)
        with patch.object(converter, "claude_summary", return_value=("[T0001] grounded", {"calls": 2, "cache_hits": 0})) as call:
            opts.summary_authorized = True
            report = self.app.execute(self.app.plan(self.get("codex", self.thread), CLAUDE, opts))
        self.assertEqual(call.call_count, 1)
        self.assertEqual(report.summary["calls"], 2)
        self.assertIn("omitted", report.to_markdown())

    def test_bounded_import_stays_under_budget_and_reports_omissions(self):
        for i in range(60):
            self.box.codex_turn(self.thread, f"large request {i}", "y" * 5000, tool=True)
        report = self.send("codex", self.thread, CLAUDE, context_budget=30000, summary_tokens=2000)
        self.assertLessEqual(report.budget["after"], 30000)
        self.assertGreater(report.budget["omitted"], 0)
        text = self.box.find_claude(report.dest_id).read_text(encoding="utf-8")
        self.assertIn("OFFLINE HISTORICAL EVIDENCE INDEX", text)
        self.assertIn("NOT an AI summary", text)
        self.assertEqual(converter.validate([json.loads(x) for x in text.splitlines()
                                      if json.loads(x).get("type") in ("user", "assistant")]), [])

    def test_replace_refused_after_claude_continues(self):
        report = self.send("codex", self.thread, CLAUDE, context_budget=20000)
        self.box.codex_turn(self.thread, "new codex", "ok")
        plan = self.app.plan(self.get("codex", self.thread), CLAUDE, self.options())
        self.assertIsNotNone(plan.option("replace"))
        replaced = self.app.execute(self.app.plan(self.get("codex", self.thread), CLAUDE,
                                                  self.options(context_budget=20000), "replace"))
        self.assertEqual(replaced.dest_id, report.dest_id)
        self.assertTrue(replaced.backups)
        self.box.claude_turn(report.dest_id, "now it has native work", "yes")
        self.box.codex_turn(self.thread, "more codex", "ok")
        plan = self.app.plan(self.get("codex", self.thread), CLAUDE, self.options())
        self.assertIsNone(plan.option("replace"))


class TestClaudeToCodex(SandboxCase):
    def test_native_session_uses_codex_importer_for_that_session_only(self):
        other = self.box.claude_session(("unrelated session", "leave me alone"))
        sid = self.box.claude_session(("plan the feature", "plan"), ("build the feature", "built"))
        report = self.send("claude", sid, CODEX)
        self.assertEqual(report.strategy, "native_import")
        self.assertEqual(self.box.bridge.imports, [str(self.box.find_claude(sid))])
        self.assertEqual(self.box.codex_humans(report.dest_id), ["plan the feature", "build the feature"])
        self.assertEqual(self.get("claude", sid).state, SyncState.UNCHANGED)
        self.assertEqual(self.get("codex", report.dest_id).state, SyncState.UNCHANGED)
        self.assertEqual(self.get("claude", other).state, SyncState.NOT_TRANSFERRED)

    def test_refresh_appends_when_thread_has_no_work(self):
        sid = self.box.claude_session(("plan the feature", "plan"))
        report = self.send("claude", sid, CODEX)
        self.box.claude_turn(sid, "build it now", "built")
        again = self.send("claude", sid, CODEX)
        self.assertEqual(again.strategy, "native_refresh")
        self.assertEqual(again.dest_id, report.dest_id)
        self.assertEqual(self.box.codex_humans(report.dest_id), ["plan the feature", "build it now"])
        self.assertEqual(self.get("claude", sid).state, SyncState.UNCHANGED)

    def test_videorenderer_scenario_is_protected(self):
        """A Claude session imported by Codex and continued there: the real thread has newer
        work than its source, so it must stay untouched and is not a duplicate."""
        sid = self.box.claude_session(("enable the GPU path", "looked at it"), ("Continue", "ok"), ("more", "ok"))
        report = self.send("claude", sid, CODEX)
        thread = report.dest_id
        for i in range(5):
            self.box.codex_turn(thread, f"continued in codex {i}", "ok", tool=True)
        rollout_before = self.box.rollout_path(thread).read_bytes()

        session = self.get("codex", thread)
        self.assertEqual(session.state, SyncState.SOURCE_ADVANCED)
        self.assertEqual(session.native_after_import, 5)
        self.assertFalse(self.app.snapshot.duplicates)
        allowed, reason = self.app.can_archive(session)
        self.assertFalse(allowed)
        self.assertIn("native Codex work", reason)
        with self.assertRaises(CleanupRefused):
            self.app.archive(session)

        # Advancing the Claude source too must not overwrite the thread or merge into it.
        self.box.claude_turn(sid, "claude moved on as well", "ok")
        claude = self.get("claude", sid)
        self.assertEqual(claude.state, SyncState.BOTH_ADVANCED)
        plan = self.app.plan(claude, CODEX, self.options())
        self.assertEqual(plan.strategy, "delta_import")
        result = self.app.execute(plan)
        self.assertNotEqual(result.dest_id, thread)
        self.assertEqual(self.box.rollout_path(thread).read_bytes(), rollout_before)
        humans = self.box.codex_humans(result.dest_id)
        self.assertIn("claude moved on as well", humans[1])
        self.assertTrue(humans[0].startswith("[ContextBridge handoff] Continue:"))
        self.assertNotIn("enable the GPU path", humans[1:])

    def test_importer_noop_is_reported_and_changes_nothing(self):
        sid = self.box.claude_session(("request one here", "answer"))
        report = self.send("claude", sid, CODEX)
        self.box.claude_turn(sid, "request two here", "answer")
        session = self.get("claude", sid)
        plan = self.app.plan(session, CODEX, self.options())
        before = self.box.rollout_path(report.dest_id).read_bytes()
        self.box.bridge.force_no_change = True
        with self.assertRaisesRegex(HandoffError, "Codex imported nothing"):
            self.app.execute(plan)
        self.assertEqual(self.box.rollout_path(report.dest_id).read_bytes(), before)
        self.assertEqual(self.get("claude", sid).state, SyncState.SOURCE_ADVANCED)

    def test_preview_goes_stale_if_codex_thread_moves(self):
        sid = self.box.claude_session(("request one here", "answer"))
        report = self.send("claude", sid, CODEX)
        self.box.claude_turn(sid, "request two here", "answer")
        plan = self.app.plan(self.get("claude", sid), CODEX, self.options())
        self.box.codex_turn(report.dest_id, "sneaked in", "ok")
        with self.assertRaisesRegex(HandoffError, "changed since this preview"):
            self.app.execute(plan)
        self.assertEqual(self.get("claude", sid).state, SyncState.BOTH_ADVANCED)

    def test_codex_unavailable_falls_back_to_assisted_steps(self):
        sid = self.box.claude_session(("request one here", "answer"))
        self.box.bridge.unavailable = True
        report = self.send("claude", sid, CODEX)
        self.assertEqual(report.status, "assisted_pending")
        self.assertTrue(any("not be reached" in f for f in report.follow_up))
        self.assertEqual(report.dest_id, "")
        # The user then imports it through Codex's own UI; the next refresh links it.
        self.box.bridge.unavailable = False
        result = self.box.bridge.import_claude_session(self.box.find_claude(sid), str(self.box.project))
        self.assertEqual(self.get("claude", sid).state, SyncState.UNCHANGED)
        self.assertEqual(self.app.store.transfer(report.transfer_id)["status"], "committed")
        self.assertEqual(self.get("codex", result.thread_id).state, SyncState.UNCHANGED)

    def test_missing_codex_thread_gets_a_whole_new_thread(self):
        sid = self.box.claude_session(("request one here", "answer"), ("request two here", "answer"))
        report = self.send("claude", sid, CODEX)
        self.box.rollout_path(report.dest_id).unlink()
        session = self.get("claude", sid)
        self.assertEqual(session.state, SyncState.MISSING_DEST)
        again = self.app.execute(self.app.plan(session, CODEX, self.options()))
        self.assertEqual(again.strategy, "delta_full")
        self.assertEqual(self.box.codex_humans(again.dest_id), ["request one here", "request two here"])

    def test_modified_claude_source_needs_an_explicit_choice(self):
        sid = self.box.claude_session(("request one here", "answer"))
        self.send("claude", sid, CODEX)
        path = self.box.find_claude(sid)
        path.write_bytes(path.read_bytes().replace(b"request one here", b"rewritten request"))
        session = self.get("claude", sid)
        self.assertEqual(session.state, SyncState.MODIFIED_SOURCE)
        plan = self.app.plan(session, CODEX, self.options())
        self.assertFalse(plan.can_execute)
        self.assertEqual(plan.strategy, "none")
        self.assertIsNotNone(plan.option("delta_full"))

    def test_missing_source_is_reported_not_guessed(self):
        sid = self.box.claude_session(("request one here", "answer"))
        report = self.send("claude", sid, CODEX)
        self.box.find_claude(sid).unlink()
        session = self.get("codex", report.dest_id)
        self.assertEqual(session.state, SyncState.MISSING_DEST)
        self.assertIn("gone", session.state_detail)


class TestRoundTrip(SandboxCase):
    def test_codex_claude_codex_claude(self):
        """The ultimate scenario: new work at each stage, with no duplicates anywhere."""
        box = self.box
        a = box.codex_rollout()
        box.codex_turn(a, "A1 start the project", "A1 done", tool=True)
        box.codex_turn(a, "A2 keep going", "A2 done")
        rollout_a = box.rollout_path(a).read_bytes()

        # Codex A -> Claude B
        b_report = self.send("codex", a, CLAUDE)
        b = b_report.dest_id
        self.assertEqual(box.claude_humans(b), ["A1 start the project", "A2 keep going"])
        box.claude_turn(b, "B1 claude takes over", "B1 done")
        box.claude_turn(b, "B2 claude continues", "B2 done")

        # Claude B -> Codex C: only B's work, with a pointer to the rest.
        self.assertEqual(self.get("claude", b).state, SyncState.SOURCE_ADVANCED)
        c_report = self.send("claude", b, CODEX)
        self.assertEqual(c_report.strategy, "delta_import")
        c = c_report.dest_id
        self.assertNotEqual(c, a)
        humans_c = box.codex_humans(c)
        self.assertTrue(humans_c[0].startswith("[ContextBridge handoff]"))
        self.assertEqual(humans_c[1:], ["B1 claude takes over", "B2 claude continues"])
        self.assertIn("OFFLINE HISTORICAL EVIDENCE INDEX", humans_c[0])
        self.assertIn(b, humans_c[0])
        self.assertTrue(Path(c_report.delta_path).is_file())
        self.assertFalse(list(box.claude_home.glob("projects/contextbridge-handoffs/*.jsonl")))
        self.assertEqual(self.get("claude", b).state, SyncState.UNCHANGED)
        box.codex_turn(c, "C1 codex resumes", "C1 done", tool=True)

        # Codex C -> Claude B again: append only C's native turn, in the same session.
        self.assertEqual(self.get("codex", c).state, SyncState.SOURCE_ADVANCED)
        d_report = self.send("codex", c, CLAUDE)
        self.assertEqual(d_report.strategy, "append")
        self.assertEqual(d_report.dest_id, b)
        final = box.claude_humans(b)
        self.assertEqual(final, ["A1 start the project", "A2 keep going", "B1 claude takes over",
                                 "B2 claude continues", "C1 codex resumes"])
        self.assertEqual(len(final), len(set(final)))

        # Nothing is pending on the latest pair, and it stays quiet.
        self.assertEqual(self.get("codex", c).state, SyncState.UNCHANGED)
        self.assertEqual(self.get("claude", b).state, SyncState.UNCHANGED)
        self.assertFalse(self.app.plan(self.get("codex", c), CLAUDE, self.options()).can_execute)
        self.assertFalse(self.app.plan(self.get("claude", b), CODEX, self.options()).can_execute)

        # Another round. C has native turns, so Codex's importer can't add to it (verified
        # against the real importer); a new thread carries just the new Claude work.
        rollout_c = box.rollout_path(c).read_bytes()
        box.claude_turn(b, "B3 claude again", "B3 done")
        e_report = self.send("claude", b, CODEX)
        self.assertEqual(e_report.strategy, "delta_import")
        self.assertNotEqual(e_report.dest_id, c)
        self.assertEqual(box.codex_humans(e_report.dest_id)[1:], ["B3 claude again"])
        self.assertEqual(box.rollout_path(c).read_bytes(), rollout_c)

        # Originals remain, one logical conversation.
        self.assertEqual(box.rollout_path(a).read_bytes(), rollout_a)
        conv = {self.get("codex", a).conversation_id, self.get("claude", b).conversation_id,
                self.get("codex", c).conversation_id}
        self.assertEqual(len(conv), 1)
        self.assertEqual(self.get("codex", a).origin_provider, CODEX)
        chain = self.app.lineage_of(self.get("claude", b))
        self.assertEqual(len(chain), 3)

    def test_delta_refresh_reaches_a_thread_without_native_work(self):
        box = self.box
        a = box.codex_rollout()
        box.codex_turn(a, "A1 start", "done")
        b = self.send("codex", a, CLAUDE).dest_id
        box.claude_turn(b, "B1 work", "done")
        report = self.send("claude", b, CODEX)
        c = report.dest_id
        box.claude_turn(b, "B2 more work", "done")
        again = self.send("claude", b, CODEX)
        self.assertEqual(again.strategy, "delta_refresh")
        self.assertEqual(again.dest_id, c)
        self.assertEqual(box.codex_humans(c)[1:], ["B1 work", "B2 more work"])
        self.assertEqual(self.get("claude", b).state, SyncState.UNCHANGED)

    def test_round_trip_into_a_separate_claude_session(self):
        """Codex A -> Claude B -> Codex C -> Claude D, each a provider-specific session."""
        box = self.box
        a = box.codex_rollout()
        box.codex_turn(a, "A1 start", "done")
        b = self.send("codex", a, CLAUDE).dest_id
        box.claude_turn(b, "B1 work", "done")
        c = self.send("claude", b, CODEX).dest_id
        box.codex_turn(c, "C1 work", "done")
        snapshot_b = box.find_claude(b).read_bytes()
        session = self.get("codex", c)
        plan = self.app.plan(session, CLAUDE, self.options(), "continuation")
        d = self.app.execute(plan).dest_id
        self.assertNotIn(d, (a, b, c))
        self.assertEqual(box.find_claude(b).read_bytes(), snapshot_b)
        humans = box.claude_humans(d)
        self.assertEqual(sum("B1 work" in h for h in humans), 1)
        self.assertEqual(humans.count("C1 work"), 1)
        self.assertEqual(len({self.get("codex", a).conversation_id,
                              self.get("claude", d).conversation_id}), 1)

    def test_lineage_database_survives_restart(self):
        box = self.box
        a = box.codex_rollout()
        box.codex_turn(a, "A1 start", "done")
        b = self.send("codex", a, CLAUDE).dest_id
        box.claude_turn(b, "B1 work", "done")
        self.send("claude", b, CODEX)
        before = [(l.src_id, l.dst_id, l.src_offset, l.dst_offset) for l in self.app.store.links()]
        self.app.store.close()
        again = box.app()
        self.addCleanup(again.store.close)
        again.refresh()
        after = [(l.src_id, l.dst_id, l.src_offset, l.dst_offset) for l in again.store.links()]
        self.assertEqual(after, before)
        self.assertEqual(again.session("claude", b).state, SyncState.UNCHANGED)


class TestDuplicates(SandboxCase):
    def circular(self, *, continued: bool):
        box = self.box
        a = box.codex_rollout()
        box.codex_turn(a, "shared opening", "answer")
        box.codex_turn(a, "shared second", "answer")
        box.codex_turn(a, "shared third", "answer")
        b = self.send("codex", a, CLAUDE).dest_id
        # Codex's own importer swept up our import, as happened with the 24 real sessions.
        thread = box.bridge.import_claude_session(box.find_claude(b), str(box.project)).thread_id
        if continued:
            box.codex_turn(thread, "real work in the copy", "answer")
        box.age(thread)
        return a, b, thread

    def test_circular_copy_is_a_cleanup_candidate(self):
        a, _b, thread = self.circular(continued=False)
        snap = self.refresh()
        hit = next(d for d in snap.duplicates if d.session == ("codex", thread))
        self.assertEqual(hit.kind, "circular_copy")
        self.assertEqual(hit.canonical, ("codex", a))
        self.assertEqual(hit.certainty, "provenance")
        self.assertTrue(hit.cleanup_allowed)
        self.assertEqual(snap.by_key[("codex", thread)].state, SyncState.POSSIBLE_DUPLICATE)
        message = self.app.archive(snap.by_key[("codex", thread)])
        self.assertIn("Archived", message)
        self.assertEqual(self.box.bridge.archived, [thread])
        self.app.restore("codex", thread)
        self.assertEqual(self.box.bridge.archived, [])

    def test_continued_circular_copy_is_protected(self):
        _a, _b, thread = self.circular(continued=True)
        snap = self.refresh()
        hit = next(d for d in snap.duplicates if d.session == ("codex", thread))
        self.assertTrue(hit.has_native_work)
        self.assertFalse(hit.cleanup_allowed)
        self.assertIn("protected", hit.reason)
        self.assertFalse(self.app.can_archive(snap.by_key[("codex", thread)])[0])

    def test_repeat_imports_are_found_from_transcript_marks_after_ledger_loss(self):
        box = self.box
        a = box.codex_rollout()
        box.codex_turn(a, "one thing to do", "answer")
        first = self.send("codex", a, CLAUDE).dest_id
        second = self.app.execute(self.app.plan(self.get("codex", a), CLAUDE, self.options(),
                                                "new_session")).dest_id
        box.claude_ledger.unlink()
        store = self.app.store
        store.conn.execute("DELETE FROM links")
        store.conn.commit()
        snap = self.refresh()
        hit = [d for d in snap.duplicates if d.kind == "repeat_import"]
        self.assertEqual(len(hit), 1)
        self.assertIn(hit[0].session[1], (first, second))

    def test_overlap_without_provenance_is_only_possible(self):
        prompts = [("alpha request here", "x"), ("beta request here", "x"), ("gamma request here", "x")]
        one = self.box.claude_session(*prompts)
        two = self.box.claude_session(*prompts)
        snap = self.refresh()
        hits = [d for d in snap.duplicates if d.session[1] in (one, two)]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].certainty, "possible")
        self.assertFalse(hits[0].cleanup_allowed)

    def test_recent_copy_is_not_offered_for_cleanup(self):
        _a, _b, thread = self.circular(continued=False)
        os.utime(self.box.rollout_path(thread))
        snap = self.refresh()
        hit = next(d for d in snap.duplicates if d.session == ("codex", thread))
        self.assertFalse(hit.cleanup_allowed)
        self.assertFalse(self.app.can_archive(snap.by_key[("codex", thread)])[0])

    def test_import_of_internal_agent_is_flagged_and_hidden(self):
        guardian = self.box.codex_rollout(source={"subagent": {"other": "guardian"}})
        self.box.codex_turn(guardian, "The following is the Codex agent history", "reviewed")
        ledger_sid = "11111111-2222-4333-8444-555555555555"
        path = self.box.claude_path(ledger_sid)
        path.parent.mkdir(parents=True)
        conv = converter.Converter({"id": guardian}, "archive", False, "2", str(self.box.project),
                            session_id=ledger_sid)
        out = conv.run([self.box.ev_user("pretend review note here"), self.box.ev_assistant("ok")])
        path.write_bytes(converter.jsonl_bytes([json.dumps(r) for r in out]))
        self.box.claude_ledger.write_text(json.dumps({guardian: {
            "claudeSessionId": ledger_sid, "transcript": str(path), "kind": "import",
            "importedAt": "2026-11-01T00:00:00+00:00", "sourceRollout": str(self.box.rollout_path(guardian)),
            "sourceBytes": 1, "sourceSha256": "x", "title": "x", "cwd": str(self.box.project)}}), encoding="utf-8")
        snap = self.refresh()
        self.assertNotIn(ledger_sid, {s.session_id for s in snap.sessions})
        self.assertTrue(snap.by_key[("claude", ledger_sid)].internal)
        hit = next(d for d in snap.duplicates if d.kind == "internal_import")
        self.assertTrue(hit.cleanup_allowed)


class TestCleanup(SandboxCase):
    def test_native_sessions_cannot_be_archived(self):
        sid = self.box.claude_session(("mine", "yes"))
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "also mine", "yes")
        snap = self.refresh()
        self.assertFalse(self.app.can_archive(snap.by_key[("claude", sid)])[0])
        self.assertFalse(self.app.can_archive(snap.by_key[("codex", thread)])[0])

    def test_claude_archive_moves_and_restores_byte_for_byte(self):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "only a copy", "yes")
        sid = self.send("codex", thread, CLAUDE).dest_id
        path = self.box.find_claude(sid)
        content = path.read_bytes()
        os.utime(path, (1, 1))
        session = self.get("claude", sid)
        session.recent = False
        self.app.archive(session)
        self.assertFalse(path.exists())
        self.assertEqual(len(self.app.store.archives()), 1)
        self.app.restore("claude", sid)
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(self.app.store.archives(), [])


class TestLedgerReconciliation(SandboxCase):
    def test_import_already_in_the_ledger_is_understood(self):
        """An import recorded before ContextBridge first ran shows up with its checkpoints."""
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "imported earlier", "answer")
        args = self.options().as_args(self.box.claude_home / "projects", dry_run=False)
        s = converter.summarize(self.box.rollout_path(thread))
        converter.resolve_lineage(s, {})
        built = converter.build_import(s, args, "2.1.300")
        target = self.box.claude_home / "projects" / converter.slug_for(built["cwd"]) / f"{built['conv'].session_id}.jsonl"
        converter.write_import(target, built["lines"])
        converter.save_ledger({thread: converter.ledger_entry(s, built, target, None, args)})
        self.assertEqual(self.get("codex", thread).state, SyncState.UNCHANGED)
        self.box.codex_turn(thread, "newer than the import", "answer")
        self.assertEqual(self.get("codex", thread).state, SyncState.SOURCE_ADVANCED)

    def test_codex_ledger_is_read_with_extended_paths(self):
        sid = self.box.claude_session(("request one here", "answer"))
        thread = self.box.bridge.import_claude_session(self.box.find_claude(sid), str(self.box.project)).thread_id
        self.assertEqual(self.get("claude", sid).state, SyncState.UNCHANGED)
        self.box.claude_turn(sid, "more here", "ok")
        self.assertEqual(self.get("claude", sid).state, SyncState.SOURCE_ADVANCED)
        self.assertEqual(self.get("codex", thread).state, SyncState.DEST_ADVANCED)

    def test_native_ledgers_are_not_rewritten_by_reading(self):
        sid = self.box.claude_session(("request one here", "answer"))
        self.box.bridge.import_claude_session(self.box.find_claude(sid), str(self.box.project))
        before = self.box.codex_ledger.read_bytes()
        self.refresh()
        self.refresh()
        self.assertEqual(self.box.codex_ledger.read_bytes(), before)
        self.assertFalse(self.box.claude_ledger.exists())

    def test_transfer_is_recorded_in_the_claude_ledger(self):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "recorded in the ledger", "answer")
        self.send("codex", thread, CLAUDE)
        ledger = converter.load_ledger()
        self.assertEqual(ledger[thread]["kind"], "import")
        self.assertEqual(ledger[thread]["sourceBytes"], self.box.rollout_path(thread).stat().st_size)


class TestWorkspace(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.box.project), "-c", "user.email=t@example.com",
                        "-c", "user.name=t", *args], check=True, capture_output=True)

    def test_reports_branch_dirty_and_never_changes_anything(self):
        self.git("init", "-b", "main")
        (self.box.project / "a.txt").write_text("1")
        self.git("add", ".")
        self.git("commit", "-m", "first")
        (self.box.project / "a.txt").write_text("2")
        (self.box.project / "b.txt").write_text("new")
        ws = inspect_workspace(str(self.box.project), recorded_branch="feature/x")
        self.assertTrue(ws.is_repo)
        self.assertEqual(ws.branch, "main")
        self.assertEqual(ws.modified, 1)
        self.assertEqual(ws.untracked, 1)
        self.assertTrue(any("feature/x" in n for n in ws.notes))
        self.assertTrue(any("Uncommitted" in n for n in ws.notes))
        self.assertEqual((self.box.project / "a.txt").read_text(), "2")

    def test_missing_directory_is_reported(self):
        ws = inspect_workspace(str(self.box.root / "gone"))
        self.assertFalse(ws.exists)
        self.assertTrue(ws.notes)

    def test_resume_commands_reject_unexpected_ids(self):
        self.assertEqual(resume_command("claude", "aaaabbbb-1452"), ["claude", "--resume", "aaaabbbb-1452"])
        self.assertEqual(resume_command("codex", "01a1cccc-3301"), ["codex", "resume", "01a1cccc-3301"])
        with self.assertRaises(ValueError):
            resume_command("claude", "x & calc.exe")
        self.assertIn("codex resume", resume_text("codex", "01a1cccc-3301", "C:\\p"))


def _tree(root: Path) -> dict:
    return {str(p): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def _offset_after_first_turn(path: Path) -> int:
    for off, rec in converter.records_from(path.read_bytes()):
        if (rec.get("payload") or {}).get("type") == "task_complete":
            return off
    raise AssertionError


if __name__ == "__main__":
    unittest.main()

