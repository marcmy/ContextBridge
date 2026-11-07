#!/usr/bin/env python3
"""The window, driven like a person would: real widgets, real backend, synthetic homes."""
import os
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from PySide6.QtCore import QCoreApplication, Qt, QThreadPool
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox, QRadioButton
except ImportError:
    raise unittest.SkipTest("PySide6 is not installed")

from unittest.mock import patch  # noqa: E402

from helpers import CLAUDE, CODEX, SandboxCase  # noqa: E402
from contextbridge.models import SyncState  # noqa: E402
from contextbridge.ui.handoff_dialog import HandoffDialog  # noqa: E402
from contextbridge.ui.main_window import MainWindow  # noqa: E402
from contextbridge.ui.session_list import SESSION_ROLE  # noqa: E402
from contextbridge.ui.theme import apply_theme  # noqa: E402


def app() -> QApplication:
    instance = QApplication.instance() or QApplication(sys.argv[:1])
    apply_theme(instance)
    return instance


def pump(until, timeout=30.0) -> None:
    """Run the event loop until a condition holds; background results arrive through it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if until():
            return
        QTest.qWait(10)
    raise AssertionError("timed out waiting for the UI")


class GuiCase(SandboxCase):
    def setUp(self):
        super().setUp()
        self.qapp = app()
        QCoreApplication.setOrganizationName("ContextBridgeTests")
        self.settings_guard()

    def settings_guard(self):
        from PySide6.QtCore import QSettings
        QSettings("ContextBridge", "ContextBridge").clear()

    def window(self) -> MainWindow:
        win = MainWindow(self.app)
        win.show()
        self.addCleanup(self.finish_window, win)
        pump(lambda: not win._refreshing and win.snapshot.sessions)
        return win

    def finish_window(self, win):
        win.close()
        QThreadPool.globalInstance().waitForDone(10000)
        QCoreApplication.processEvents()

    def wait_refreshed(self, win):
        pump(lambda: not win._refreshing)
        QThreadPool.globalInstance().waitForDone(5000)
        pump(lambda: not win._refreshing)


class TestMainWindow(GuiCase):
    def setUp(self):
        super().setUp()
        self.thread = self.box.codex_rollout()
        self.box.codex_turn(self.thread, "Refactor the renderer", "done", tool=True)
        self.other = self.box.claude_session(("Write the installer", "done"))

    def test_lists_both_agents_and_filters(self):
        win = self.window()
        self.assertEqual(win.tree.visible_count(), 2)
        win.provider_filter.setCurrentIndex(win.provider_filter.findData("codex"))
        pump(lambda: win.tree.visible_count() == 1)
        win.provider_filter.setCurrentIndex(0)
        win.search.setText("installer")
        pump(lambda: win.tree.visible_count() == 1)
        win.search.setText("no such thing at all")
        pump(lambda: win.tree.visible_count() == 0)
        win.search.clear()
        pump(lambda: win.tree.visible_count() == 2)
        self.assertIn("Codex", win.status_text.text())

    def test_session_details_show_state_and_lineage(self):
        win = self.window()
        self.assertTrue(win.tree.select(("codex", self.thread)))
        pump(lambda: win.details.session is not None)
        self.assertEqual(win.details.send.text(), "Send to Claude")
        self.assertIn("Refactor the renderer", win.details.title.text())
        win.details.tabs.setCurrentIndex(2)
        pump(lambda: "Checking" not in win.details.workspace_notes.text())
        self.assertTrue(win.details.workspace_notes.text())

    def test_internal_agents_toggle(self):
        self.box.codex_rollout(source={"subagent": {"other": "guardian"}})
        win = self.window()
        self.assertEqual(win.tree.visible_count(), 2)
        win.show_internal.setChecked(True)
        pump(lambda: not win._refreshing and win.tree.visible_count() == 3)

    def test_problem_banner_shows_interrupted_transfers(self):
        self.app.store.begin_transfer("abc", strategy="append", src=("codex", self.thread),
                                      dst=("claude", "x"), dst_path="p", intent={})
        win = self.window()
        self.assertTrue(win.problem_banner.isVisibleTo(win))
        self.assertIn("interrupted", win.problem_text.text())


class TestHandoffDialog(GuiCase):
    def dialog(self, session, target) -> HandoffDialog:
        dialog = HandoffDialog(self.app, session, target)
        dialog.show()
        self.addCleanup(lambda: (dialog.close(), QThreadPool.globalInstance().waitForDone(10000)))
        pump(lambda: dialog.plan is not None or "Calculating" not in dialog.progress_text.text())
        pump(lambda: not dialog._busy)
        return dialog

    def click_through(self, dialog) -> None:
        QTest.mouseClick(dialog.next_button, Qt.LeftButton)     # to Context
        pump(lambda: dialog.pages.currentIndex() == 1)
        QTest.mouseClick(dialog.next_button, Qt.LeftButton)     # to Confirm
        pump(lambda: dialog.pages.currentIndex() == 2)
        self.assertEqual(dialog.next_button.text(), "Start transfer")
        QTest.mouseClick(dialog.next_button, Qt.LeftButton)
        pump(lambda: dialog.report is not None)

    def test_codex_to_claude_through_the_dialog(self):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "Refactor the renderer", "done", tool=True)
        session = self.get("codex", thread)
        dialog = self.dialog(session, CLAUDE)
        self.assertEqual(dialog.plan.strategy, "new_session")
        self.assertIn("estimated tokens", dialog.numbers_text.text())
        self.click_through(dialog)
        self.assertEqual(dialog.report.status, "committed")
        self.assertTrue(dialog.open_destination.isVisibleTo(dialog))
        self.assertTrue(Path(dialog.report.report_path).is_file())
        self.assertEqual(self.box.claude_humans(dialog.report.dest_id), ["Refactor the renderer"])
        self.assertEqual(self.get("codex", thread).state, SyncState.UNCHANGED)

    def test_claude_to_codex_through_the_dialog(self):
        sid = self.box.claude_session(("Write the installer", "done"))
        dialog = self.dialog(self.get("claude", sid), CODEX)
        self.assertEqual(dialog.plan.strategy, "native_import")
        self.assertTrue(dialog.mode.isHidden())
        self.click_through(dialog)
        self.assertEqual(dialog.report.status, "committed")
        self.assertEqual(self.box.codex_humans(dialog.report.dest_id), ["Write the installer"])

    def test_unchanged_session_cannot_start_a_transfer(self):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "Refactor the renderer", "done")
        self.send("codex", thread, CLAUDE)
        dialog = self.dialog(self.get("codex", thread), CLAUDE)
        self.assertEqual(dialog.plan.strategy, "none")
        self.assertFalse(dialog.next_button.isEnabled())

    def test_both_advanced_recommends_a_continuation(self):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "Refactor the renderer", "done")
        dest = self.send("codex", thread, CLAUDE).dest_id
        self.box.codex_turn(thread, "more codex", "done")
        self.box.claude_turn(dest, "more claude", "done")
        dialog = self.dialog(self.get("codex", thread), CLAUDE)
        self.assertEqual(dialog.plan.strategy, "continuation")
        radios = {r.property("strategy"): r for r in dialog.findChildren(QRadioButton)}
        self.assertFalse(radios["append"].isEnabled())
        self.assertTrue(radios["continuation"].isChecked())
        self.assertTrue(radios["merge"].isEnabled())
        self.assertFalse(radios["merge"].isChecked())
        radios["merge"].setChecked(True)
        pump(lambda: dialog.plan.strategy == "merge" and not dialog._busy)
        self.assertTrue(any("merges two diverged" in w for w in dialog.plan.warnings))

    def test_changing_the_budget_recalculates_the_preview(self):
        thread = self.box.codex_rollout()
        for i in range(30):
            self.box.codex_turn(thread, f"large request {i}", "y" * 4000, tool=True)
        dialog = self.dialog(self.get("codex", thread), CLAUDE)
        first = dialog.plan.estimated_after
        index = dialog.budget.findData(-1)
        dialog.budget_custom.setValue(20000)
        dialog.budget.setCurrentIndex(index)
        pump(lambda: dialog.options.context_budget == 20000 and not dialog._busy
             and dialog.plan.options.context_budget == 20000)
        self.assertLess(dialog.plan.estimated_after, first)
        self.assertLessEqual(dialog.plan.budget["after"], 20000)

    def test_ai_summary_needs_the_authorization_box(self):
        thread = self.box.codex_rollout()
        for i in range(30):
            self.box.codex_turn(thread, f"large request {i}", "y" * 4000, tool=True)
        dialog = self.dialog(self.get("codex", thread), CLAUDE)
        dialog.budget_custom.setValue(20000)
        dialog.budget.setCurrentIndex(dialog.budget.findData(-1))
        dialog.summary_mode.setCurrentIndex(dialog.summary_mode.findData("claude"))
        pump(lambda: not dialog._busy and dialog.plan.options.summary_mode == "claude")
        self.assertFalse(dialog.ai_frame.isHidden())
        dialog._go(2)
        self.assertFalse(dialog.next_button.isEnabled())
        dialog.ai_authorized.setChecked(True)
        pump(lambda: dialog.next_button.isEnabled())

    def test_failure_is_shown_and_nothing_changes(self):
        thread = self.box.codex_rollout()
        self.box.codex_turn(thread, "Refactor the renderer", "done")
        session = self.get("codex", thread)
        dialog = self.dialog(session, CLAUDE)
        self.box.codex_turn(thread, "moved on behind the preview", "done")
        self.click_through(dialog)
        self.assertEqual(dialog.report.status, "failed")
        self.assertIn("changed since this preview", dialog.result_text.toPlainText())
        self.assertEqual(len(list(self.box.claude_home.glob("projects/*/*.jsonl"))), 0)


class TestManagePage(GuiCase):
    def test_transfers_duplicates_and_archive(self):
        box = self.box
        a = box.codex_rollout()
        for text in ("shared opening", "shared second", "shared third"):
            box.codex_turn(a, text, "answer")
        b = self.send("codex", a, CLAUDE).dest_id
        thread = box.bridge.import_claude_session(box.find_claude(b), str(box.project)).thread_id
        box.age(thread)
        win = self.window()
        page = win.manage
        self.assertEqual(page.transfer_table.rowCount(), 1)
        self.assertEqual(page.transfer_table.item(0, 1).text(), "Completed")
        self.assertGreaterEqual(page.duplicate_table.rowCount(), 1)
        page.duplicate_table.setCurrentCell(0, 0)
        self.assertTrue(page.archive_button.isEnabled())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
            page._archive_selected()
            pump(lambda: box.bridge.archived == [thread])
            self.wait_refreshed(win)
            pump(lambda: page.archive_table.rowCount() == 1)
            page.archive_table.setCurrentCell(0, 0)
            page._restore_selected()
            pump(lambda: box.bridge.archived == [] and page.archive_table.rowCount() == 0)
            self.wait_refreshed(win)

    def test_protected_session_cannot_be_archived_from_the_page(self):
        box = self.box
        a = box.codex_rollout()
        for text in ("shared opening", "shared second", "shared third"):
            box.codex_turn(a, text, "answer")
        b = self.send("codex", a, CLAUDE).dest_id
        thread = box.bridge.import_claude_session(box.find_claude(b), str(box.project)).thread_id
        box.codex_turn(thread, "real work", "answer")
        win = self.window()
        page = win.manage
        for row in range(page.duplicate_table.rowCount()):
            if page.duplicate_table.item(row, 0).data(Qt.UserRole).session == ("codex", thread):
                page.duplicate_table.setCurrentCell(row, 0)
                break
        self.assertFalse(page.archive_button.isEnabled())
        self.assertEqual(page.duplicate_table.item(page.duplicate_table.currentRow(), 4).text(), "Protected")


class TestMaintenancePages(GuiCase):
    def test_import_health_finds_and_removes_an_import(self):
        box = self.box
        thread = box.codex_rollout()
        box.codex_turn(thread, "a request worth keeping", "answer")
        sid = self.send("codex", thread, CLAUDE).dest_id
        path = box.find_claude(sid)
        stamp = path.stat().st_mtime - 3600
        os.utime(path, (stamp, stamp))
        win = self.window()
        page = win.manage
        page.tabs.setCurrentWidget(page.imports)
        pump(lambda: page.imports.table.rowCount() == 1)
        self.assertEqual(page.imports.table.item(0, 4).text(), "Intact")
        self.assertIn("1 intact", page.imports.summary.text())
        page.imports.table.selectRow(0)
        self.assertTrue(page.imports.remove_button.isEnabled())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch.object(QMessageBox, "information", return_value=QMessageBox.Ok) as told:
            page.imports._remove()
            pump(lambda: not path.exists())
            pump(lambda: told.called)
            self.wait_refreshed(win)
            pump(lambda: page.archive_table.rowCount() == 1)

    def test_bulk_import_through_the_page(self):
        box = self.box
        thread = box.codex_rollout()
        box.codex_turn(thread, "first thread", "answer")
        other = box.codex_rollout()
        box.codex_turn(other, "second thread", "answer")
        win = self.window()
        panel = win.manage.bulk
        self.assertEqual(panel.table.rowCount(), 2)
        panel.table.item(1, 0).setCheckState(Qt.Unchecked)
        panel.budget.setCurrentIndex(panel.budget.findData(None))
        panel.prefix.setText("[codex] ")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch.object(QMessageBox, "information", return_value=QMessageBox.Ok) as told:
            panel._start()
            pump(lambda: told.called)
            self.wait_refreshed(win)
        self.assertIn("Imported: 1 of 1", told.call_args[0][2])
        written = list(box.claude_home.glob("projects/*/*.jsonl"))
        self.assertEqual(len(written), 1)
        self.assertIn("[codex] ", written[0].read_text(encoding="utf-8"))
        pump(lambda: panel.table.rowCount() == 1)

    def test_desktop_page_clears_rows_and_copies_between_accounts(self):
        box = self.box
        keep = box.claude_session(("a session to keep", "ok"))
        other = box.claude_session(("in the new account", "ok"))
        dead = box.desktop_card("old", "work", "gone-session", title="Dead row")
        box.desktop_card("old", "work", keep, title="Live row")
        box.desktop_card("new", "work", other)
        box.set_active_account("new")
        win = self.window()
        page = win.manage
        panel = page.desktop
        page.tabs.setCurrentWidget(panel)
        pump(lambda: panel.dead_table.rowCount() == 1)
        self.assertEqual(panel.dead_table.item(0, 0).text(), "Dead row")
        self.assertEqual(panel.source.currentData().parent.name, "old")
        self.assertEqual(panel.destination.currentData().parent.name, "new")
        self.assertTrue(panel.move_button.isEnabled())
        new_reg = panel.destination.currentData()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes), \
                patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
            panel._clear()
            pump(lambda: not dead.exists())
            self.wait_refreshed(win)
            pump(lambda: panel.dead_table.rowCount() == 0 and panel.refresh_button.isEnabled())
            panel._move()
            pump(lambda: len(list(new_reg.glob("local_*.json"))) == 2)
            self.wait_refreshed(win)
            pump(lambda: panel.refresh_button.isEnabled())

    def test_dialog_carries_the_conversion_options(self):
        box = self.box
        thread = box.codex_rollout()
        box.codex_turn(thread, "a request", "answer")
        win = self.window()
        session = next(s for s in win.snapshot.sessions if s.session_id == thread)
        dialog = HandoffDialog(self.app, session, CLAUDE, win)
        self.addCleanup(dialog.deleteLater)
        pump(lambda: dialog.plan is not None)
        dialog.title_prefix.setText("[codex] ")
        dialog.include_banner.setChecked(False)
        dialog.group_scratch.setChecked(False)
        self.assertFalse(dialog.summary_file.isVisibleTo(dialog.summary_file.parentWidget()))
        dialog.summary_mode.setCurrentIndex(dialog.summary_mode.findData("file"))
        self.assertTrue(dialog.summary_file.isVisibleTo(dialog.summary_file.parentWidget()))
        dialog.summary_file.setText("notes.txt")
        options = dialog._read_form()
        self.assertEqual(options.title_prefix, "[codex] ")
        self.assertFalse(options.include_banner)
        self.assertFalse(options.group_scratch)
        self.assertEqual((options.summary_mode, options.summary_file), ("file", "notes.txt"))
        dialog.summary_mode.setCurrentIndex(dialog.summary_mode.findData("index"))
        pump(lambda: not dialog._busy)


if __name__ == "__main__":
    unittest.main()
