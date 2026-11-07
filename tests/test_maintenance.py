#!/usr/bin/env python3
"""Import health, removal, bulk import, Claude Desktop rows and the conversion options."""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import CLAUDE, CODEX, SandboxCase  # noqa: E402
from contextbridge import converter, desktop_register  # noqa: E402
from contextbridge.handoff import HandoffError, HandoffOptions  # noqa: E402
from contextbridge.models import SyncState  # noqa: E402
from contextbridge.paths import Roots  # noqa: E402


class MaintenanceCase(SandboxCase):
    def imported(self, text="a request worth keeping"):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, text, "answer")
        return thread, self.send("codex", thread, CLAUDE).dest_id

    def settle(self, sid):
        """Make a Claude transcript look settled rather than written a moment ago."""
        path = self.box.find_claude(sid)
        stamp = path.stat().st_mtime - 3600
        os.utime(path, (stamp, stamp))

    def record(self, sid):
        return next(r for r in self.app.maintenance.imports() if r.claude_id == sid)


class TestImportHealth(MaintenanceCase):
    def test_a_fresh_import_is_intact(self):
        thread, sid = self.imported()
        record = self.record(sid)
        self.assertTrue(record.healthy, record.problems)
        self.assertEqual(record.thread_id, thread)
        self.assertEqual(record.cards, [])

    def test_missing_transcript_is_reported(self):
        _thread, sid = self.imported()
        self.box.find_claude(sid).unlink()
        self.assertIn("The Claude transcript is missing.", self.record(sid).problems)

    def test_newer_codex_turns_are_reported(self):
        thread, sid = self.imported()
        self.box.codex_turn(thread, "a newer turn", "answer")
        self.assertIn("Codex has newer turns than this import.", self.record(sid).problems)

    def test_deleted_source_rollout_is_reported(self):
        thread, sid = self.imported()
        self.box.rollout_path(thread).unlink()
        self.assertIn("The source Codex rollout is gone.", self.record(sid).problems)

    def test_import_missing_from_the_ledger_is_found_by_its_marker(self):
        _thread, sid = self.imported()
        converter.save_ledger({})
        record = self.record(sid)
        self.assertFalse(record.in_ledger)
        self.assertFalse(record.healthy)

    def test_native_session_is_not_mistaken_for_an_import(self):
        native = self.box.claude_session(("my own work", "ok"))
        self.assertNotIn(native, [r.claude_id for r in self.app.maintenance.imports()])

    def test_checking_changes_nothing(self):
        _thread, sid = self.imported()
        path = self.box.find_claude(sid)
        before = path.read_bytes(), self.box.claude_ledger.read_bytes()
        self.app.maintenance.imports()
        self.assertEqual((path.read_bytes(), self.box.claude_ledger.read_bytes()), before)


class TestRemoveImports(MaintenanceCase):
    def test_removal_is_undone_completely_by_restore(self):
        thread, sid = self.imported()
        card = self.box.desktop_card("acct", "work", sid)
        path = self.box.find_claude(sid)
        content, ledger = path.read_bytes(), converter.load_ledger()
        result = self.app.maintenance.remove([self.record(sid)])
        self.assertEqual(len(result.moved), 1)
        self.assertFalse(path.exists())
        self.assertFalse(card.exists())
        self.assertNotIn(thread, converter.load_ledger())
        self.assertEqual(self.get("codex", thread).state, SyncState.MISSING_DEST)
        self.assertTrue(list((self.box.app_home / "backups").glob("codex-imports-*.json")))

        self.assertIn("Restored", self.app.restore("claude", sid))
        self.assertEqual(path.read_bytes(), content)
        self.assertTrue(card.exists())
        self.assertEqual(converter.load_ledger(), ledger)
        self.assertEqual(self.get("codex", thread).state, SyncState.UNCHANGED)

    def test_native_work_is_protected(self):
        _thread, sid = self.imported()
        self.box.claude_turn(sid, "work done in claude", "ok")
        path = self.box.find_claude(sid)
        before = path.read_bytes()
        result = self.app.maintenance.remove([self.record(sid)])
        self.assertEqual(result.moved, [])
        self.assertIn("native Claude work", result.refused[0][1])
        self.assertEqual(path.read_bytes(), before)

    def test_session_in_use_is_refused(self):
        _thread, sid = self.imported()
        self.refresh()
        self.app.session("claude", sid).active = True
        result = self.app.remove_imports([self.record(sid)])
        self.assertIn("in use", result.refused[0][1])
        self.assertTrue(self.box.find_claude(sid).exists())

    def test_settled_session_can_be_removed_through_the_service(self):
        _thread, sid = self.imported()
        self.settle(sid)
        self.refresh()
        self.assertEqual(len(self.app.remove_imports([self.record(sid)]).moved), 1)

    def test_round_trip_keeps_the_native_session(self):
        thread, sid = self.imported()
        ledger = converter.load_ledger()
        ledger[thread]["kind"] = "round-trip"
        converter.save_ledger(ledger)
        self.box.claude_turn(sid, "my own work", "ok")
        path = self.box.find_claude(sid)
        before = path.read_bytes()
        result = self.app.maintenance.remove([self.record(sid)])
        self.assertEqual(len(result.record_only), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(thread, converter.load_ledger())

    def test_import_found_by_marker_can_be_removed_and_restored(self):
        _thread, sid = self.imported()
        converter.save_ledger({})
        path = self.box.find_claude(sid)
        self.assertEqual(len(self.app.maintenance.remove([self.record(sid)]).moved), 1)
        self.assertFalse(path.exists())
        self.app.restore("claude", sid)
        self.assertTrue(path.exists())


class TestDeadCards(MaintenanceCase):
    def test_rows_without_a_transcript_are_cleared_and_can_come_back(self):
        _thread, sid = self.imported()
        live = self.box.desktop_card("acct", "work", sid)
        dead = self.box.desktop_card("acct", "work", "gone-session", title="Old row")
        tools = self.app.maintenance
        self.assertEqual([c.path for c in tools.dead_cards()], [dead])
        self.assertEqual(tools.clear_dead_cards(tools.dead_cards()), 1)
        self.assertFalse(dead.exists())
        self.assertTrue(live.exists())
        self.assertEqual(tools.dead_cards(), [])
        self.assertEqual(tools.restore_cards(), 1)
        self.assertTrue(dead.exists())

    def test_nothing_is_called_dead_when_there_are_no_transcripts(self):
        self.box.desktop_card("acct", "work", "any-session")
        self.assertEqual(self.app.maintenance.dead_cards(), [])

    def test_row_of_an_archived_session_is_left_alone(self):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "first request", "answer")
        sid = self.send("codex", thread, CLAUDE).dest_id
        self.box.claude_session(("something else", "ok"))
        self.box.desktop_card("acct", "work", sid)
        self.settle(sid)
        self.app.archive(self.get("claude", sid))
        self.assertEqual(self.app.maintenance.dead_cards(), [])


class TestMoveAccount(MaintenanceCase):
    def test_copies_rows_and_skips_what_it_cannot(self):
        thread, sid = self.imported()
        other = self.box.claude_session(("second", "ok"))
        template = self.box.desktop_card("new", "work", other, toolSurfaceSnapshot="new",
                                         remoteMcpServersConfig=["new"])
        old = self.box.desktop_card("old", "work", sid, title="Keep this title", model="m1",
                                    toolSurfaceSnapshot="old", remoteMcpServersConfig=["old"],
                                    envScopeId="old-env", error="boom")
        self.box.desktop_card("old", "work", "gone-session")
        self.box.desktop_card("old", "work", other)
        tools = self.app.maintenance
        rows = tools.plan_move(old.parent, template.parent)
        self.assertEqual(sorted(r.action for r in rows), ["already_there", "copy", "no_transcript"])
        self.assertEqual(len(list(template.parent.glob("*.json"))), 1)   # a plan writes nothing

        result = tools.move_cards(old.parent, template.parent)
        self.assertEqual(result, {"copied": 1, "already_there": 1, "no_transcript": 1})
        self.assertTrue(old.exists())
        card_path = desktop_register.cards_by_cli_id(template.parent)[sid]
        card = json.loads(card_path.read_text(encoding="utf-8"))
        self.assertEqual(card["title"], "Keep this title")
        self.assertEqual(card["model"], "m1")
        self.assertEqual(card["toolSurfaceSnapshot"], "new")
        self.assertEqual(card["remoteMcpServersConfig"], ["new"])
        self.assertNotIn("envScopeId", card)
        self.assertNotIn("error", card)
        self.assertEqual(converter.load_ledger()[thread]["card"], str(card_path))
        # Safe to repeat.
        self.assertEqual(tools.move_cards(old.parent, template.parent)["copied"], 0)

    def test_taking_rows_out_of_the_old_account_keeps_them_recoverable(self):
        _thread, sid = self.imported()
        other = self.box.claude_session(("second", "ok"))
        template = self.box.desktop_card("new", "work", other)
        old = self.box.desktop_card("old", "work", sid)
        tools = self.app.maintenance
        tools.move_cards(old.parent, template.parent, remove_source=True)
        self.assertFalse(old.exists())
        self.assertEqual(len(tools.archived_cards()), 1)
        self.assertEqual(tools.restore_cards(), 1)
        self.assertTrue(old.exists())

    def test_same_account_is_refused(self):
        card = self.box.desktop_card("acct", "work", "a-session")
        with self.assertRaises(ValueError):
            self.app.maintenance.plan_move(card.parent, card.parent)

    def test_accounts_mark_the_one_signed_in(self):
        self.box.desktop_card("old", "work", "a-session")
        self.box.desktop_card("new", "work", "b-session")
        self.box.set_active_account("new")
        accounts = self.app.maintenance.accounts()
        self.assertEqual(len(accounts), 2)
        self.assertEqual([a.path.parent.name for a in accounts if a.current], ["new"])


class TestDesktopIsolation(unittest.TestCase):
    def test_another_claude_home_never_sees_the_real_cards(self):
        real = desktop_register.SESSIONS
        self.addCleanup(desktop_register.configure_paths, None)
        Roots(Path("sandbox/codex"), Path("sandbox/claude"), Path("sandbox/app")).activate()
        self.assertNotEqual(desktop_register.SESSIONS, real)
        self.assertEqual(desktop_register.SESSIONS, Path("sandbox/app/no-desktop/claude-code-sessions"))


class TestBulkImport(MaintenanceCase):
    def options(self, **kw):
        kw.setdefault("context_budget", None)
        return HandoffOptions(**kw)

    def test_rows_say_what_would_happen_and_skip_diverged_threads(self):
        fresh = self.box.codex_rollout()
        self.box.codex_turn(fresh, "never transferred", "answer")
        ahead, _ = self.imported("moved on in codex")
        self.box.codex_turn(ahead, "newer", "answer")
        split, split_sid = self.imported("moved on in both")
        self.box.claude_turn(split_sid, "claude work", "ok")
        self.box.codex_turn(split, "codex work", "answer")
        same, _ = self.imported("nothing new")
        gone, gone_sid = self.imported("lost its transcript")
        self.box.find_claude(gone_sid).unlink()
        self.refresh()
        rows = {r.session.session_id: r for r in self.app.bulk_rows()}
        self.assertNotIn(same, rows)
        self.assertTrue(rows[fresh].checked)
        self.assertTrue(rows[ahead].checked)
        self.assertTrue(rows[split].selectable)
        self.assertFalse(rows[split].checked)
        self.assertIn("merge", rows[split].action)
        self.assertTrue(rows[gone].selectable)
        self.assertFalse(rows[gone].checked)

    def test_imports_new_threads_and_appends_to_existing_ones(self):
        fresh = self.box.codex_rollout()
        self.box.codex_turn(fresh, "never transferred", "answer")
        ahead, sid = self.imported("already there")
        self.box.codex_turn(ahead, "newer turn", "answer")
        self.refresh()
        chosen = [r.session for r in self.app.bulk_rows() if r.checked]
        results = self.app.bulk_import(chosen, self.options())
        self.assertEqual(sorted(r.status for r in results), ["committed", "committed"])
        self.assertEqual(self.box.claude_humans(sid), ["already there", "newer turn"])
        new_id = next(r.dest_id for r in results if r.session.session_id == fresh)
        self.assertEqual(self.box.claude_humans(new_id), ["never transferred"])
        self.refresh()
        self.assertEqual([r for r in self.app.bulk_rows() if r.checked], [])

    def test_one_thread_that_cannot_go_does_not_stop_the_others(self):
        first = self.box.codex_rollout()
        self.box.codex_turn(first, "first thread", "answer")
        second = self.box.codex_rollout()
        self.box.codex_turn(second, "second thread", "answer")
        self.refresh()
        sessions = [r.session for r in self.app.bulk_rows() if r.checked]
        self.box.rollout_path(sessions[0].session_id).write_text("not json\n", encoding="utf-8")
        results = self.app.bulk_import(sessions, self.options())
        self.assertEqual(len(results), 2)
        self.assertNotEqual(results[0].status, "committed")
        self.assertEqual(results[1].status, "committed")

    def test_ai_summaries_are_refused(self):
        with self.assertRaises(HandoffError):
            self.app.bulk_import([], self.options(context_budget=20000, summary_mode="claude"))


class TestMergeDiverged(MaintenanceCase):
    def diverged(self):
        thread, sid = self.imported("the shared start")
        self.box.claude_turn(sid, "work done in claude", "ok")
        self.box.codex_turn(thread, "work done in codex", "ok")
        return thread, sid

    def merged(self):
        thread, sid = self.diverged()
        before = self.box.find_claude(sid).read_bytes()
        report = self.send("codex", thread, CLAUDE, "merge")
        self.assertEqual(report.status, "committed")
        return thread, sid, before, report

    def test_it_is_offered_but_never_chosen_for_you(self):
        thread, _sid = self.diverged()
        plan = self.app.plan(self.get("codex", thread), CLAUDE, self.options())
        self.assertEqual(plan.strategy, "continuation")
        self.assertTrue(plan.option("merge").enabled)
        self.assertFalse(plan.option("merge").recommended)

    def test_keeps_both_branches_after_a_backup(self):
        _thread, sid, before, report = self.merged()
        path = self.box.find_claude(sid)
        self.assertEqual(report.dest_id, sid)
        self.assertTrue(path.read_bytes().startswith(before))
        self.assertEqual(self.box.claude_humans(sid),
                         ["the shared start", "work done in claude", "work done in codex"])
        self.assertEqual(Path(report.backups[0]).read_bytes(), before)

    def test_claude_work_still_shows_as_not_in_codex(self):
        thread, sid, _before, _report = self.merged()
        self.assertEqual(self.get("codex", thread).state, SyncState.DEST_ADVANCED)
        self.assertEqual(self.get("claude", sid).state, SyncState.SOURCE_ADVANCED)
        self.refresh()
        self.assertEqual(self.get("codex", thread).state, SyncState.DEST_ADVANCED)

    def test_sending_back_carries_only_claudes_own_turns(self):
        _thread, sid, _before, _report = self.merged()
        back = self.send("claude", sid, CODEX, "delta_import")
        self.assertEqual(self.box.codex_humans(back.dest_id)[1:], ["work done in claude"])

    def test_can_merge_again_after_more_codex_work(self):
        thread, sid, _before, _report = self.merged()
        self.box.codex_turn(thread, "even more in codex", "ok")
        session = self.get("codex", thread)
        self.assertEqual(session.state, SyncState.BOTH_ADVANCED)
        self.send("codex", thread, CLAUDE, "merge")
        self.assertEqual(self.box.claude_humans(sid)[-2:], ["work done in codex", "even more in codex"])
        back = self.send("claude", sid, CODEX, "delta_import")
        self.assertEqual(self.box.codex_humans(back.dest_id)[1:], ["work done in claude"])

    def test_a_session_in_use_is_blocked(self):
        thread, sid = self.diverged()
        session = self.get("codex", thread)
        self.app.session("claude", sid).active = True
        plan = self.app.plan(session, CLAUDE, self.options(), "merge")
        self.assertFalse(plan.can_execute)
        self.assertIn("in use", plan.blocked)

    def test_a_session_that_changed_after_the_preview_is_refused(self):
        thread, sid = self.diverged()
        plan = self.app.plan(self.get("codex", thread), CLAUDE, self.options(), "merge")
        self.box.claude_turn(sid, "typed after the preview", "ok")
        before = self.box.find_claude(sid).read_bytes()
        with self.assertRaises(HandoffError):
            self.app.execute(plan)
        self.assertEqual(self.box.find_claude(sid).read_bytes(), before)

    def test_without_a_claude_position_it_is_blocked(self):
        thread, sid = self.diverged()
        plan = self.app.plan(self.get("codex", thread), CLAUDE, self.options(), "merge")
        link = plan.assessment.link
        side = link.side(CLAUDE, sid)
        link.set_position(side, link.position(side)[0], None, None)
        self.app.engine._measure(plan, self.app.snapshot.by_key)
        self.assertIn("no recorded position", plan.blocked)

    def test_not_offered_when_the_source_was_rewritten(self):
        thread, _sid = self.imported("the shared start")
        path = self.box.rollout_path(thread)
        path.write_bytes(path.read_bytes().replace(b"the shared start", b"THE SHARED START"))
        plan = self.app.plan(self.get("codex", thread), CLAUDE, self.options())
        self.assertIsNone(plan.option("merge"))

    def test_bulk_import_merges_only_a_ticked_thread(self):
        thread, sid = self.diverged()
        self.refresh()
        self.assertEqual([r for r in self.app.bulk_rows() if r.checked], [])
        session = next(r.session for r in self.app.bulk_rows() if r.session.session_id == thread)
        results = self.app.bulk_import([session], self.options())
        self.assertEqual(results[0].status, "committed")
        self.assertIn("work done in codex", self.box.claude_humans(sid))


class TestConversionOptions(MaintenanceCase):
    def long_thread(self):
        thread = self.box.codex_rollout()
        for i in range(12):
            self.box.codex_turn(thread, f"request {i} " + "x" * 6000, "answer " + "y" * 6000)
        return thread

    def text_of(self, sid):
        return self.box.find_claude(sid).read_text(encoding="utf-8")

    def test_title_prefix_is_applied(self):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "a titled request", "answer")
        sid = self.send("codex", thread, CLAUDE, title_prefix="[codex] ").dest_id
        self.assertIn('"customTitle": "[codex] a titled request"', self.text_of(sid))

    def test_banner_can_be_left_out_and_provenance_stays(self):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "no banner please", "answer")
        sid = self.send("codex", thread, CLAUDE, include_banner=False).dest_id
        text = self.text_of(sid)
        self.assertNotIn("Imported from OpenAI Codex", text)
        self.assertIn('"importedFrom": "codex-import"', text)

    def test_scratch_folder_grouping_can_be_turned_off(self):
        scratch = self.box.root / "Documents" / "Codex" / "2026-11-01" / "a-thread"
        scratch.mkdir(parents=True)
        thread = self.box.codex_rollout(cwd=scratch)
        self.box.codex_turn(thread, "from a scratch folder", "answer")
        kept = self.send("codex", thread, CLAUDE, group_scratch=False).dest_id
        self.assertEqual(self.box.find_claude(kept).parent.name, converter.slug_for(str(scratch)))

    def test_summary_from_a_file_reaches_the_session(self):
        notes = self.box.root / "notes.txt"
        notes.write_text("A REVIEWED SUMMARY OF EARLIER WORK", encoding="utf-8")
        thread = self.long_thread()
        sid = self.send("codex", thread, CLAUDE, context_budget=9000, summary_tokens=1500,
                        summary_mode="file", summary_file=str(notes)).dest_id
        self.assertIn("A REVIEWED SUMMARY OF EARLIER WORK", self.text_of(sid))

    def test_summary_file_reaches_a_handoff_to_codex(self):
        notes = self.box.root / "notes.txt"
        notes.write_text("A REVIEWED SUMMARY OF EARLIER WORK", encoding="utf-8")
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "earlier codex work", "answer")
        sid = self.send("codex", thread, CLAUDE).dest_id
        self.box.claude_turn(sid, "newer claude work", "ok")
        report = self.send("claude", sid, CODEX, summary_mode="file", summary_file=str(notes))
        self.assertIn("A REVIEWED SUMMARY OF EARLIER WORK", self.box.codex_humans(report.dest_id)[0])

    def test_missing_summary_file_is_refused_before_anything_is_written(self):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "a request", "answer")
        session = self.get("codex", thread)
        with self.assertRaisesRegex(HandoffError, "existing text file"):
            self.app.plan(session, CLAUDE, self.options(summary_mode="file", summary_file="nope.txt"))


if __name__ == "__main__":
    unittest.main()
