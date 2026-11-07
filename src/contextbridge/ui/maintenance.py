"""The pages that look after imports: health and removal, bulk import, Claude Desktop rows."""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFormLayout, QHBoxLayout,
                               QLineEdit, QMessageBox, QProgressBar, QPushButton, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ..handoff import HandoffOptions
from ..maintenance import ImportRecord
from ..models import SyncState
from ..service import ContextBridge, Snapshot
from .details import label
from .handoff_dialog import BUDGETS, saved_options
from .manage_table import cell, table
from .theme import colors
from .workers import run_task

DESKTOP_NOTE = ("\n\nClaude Desktop is running. It keeps its session list in memory and can write "
                "rows back, so quit it first if you can, and reopen it afterwards.")


def _relaunch(running: bool) -> str:
    return DESKTOP_NOTE if running else "\n\nReopen Claude Desktop afterwards so its sidebar catches up."


class ImportsPanel(QWidget):
    """What the old status, uninstall and repair reports covered."""
    changed = Signal()
    show_session = Signal(object)

    def __init__(self, bridge: ContextBridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.records: list[ImportRecord] = []
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 8, 0, 0)
        box.addWidget(label("Every import ContextBridge can find, including ones missing from the "
                            "ledger, and whether each one is still intact. Removing moves a "
                            "transcript and its sidebar rows to the Archived tab, where Restore "
                            "puts them back. Sessions with native Claude work are protected.",
                            "muted", wrap=True))
        self.summary = label("", "muted", wrap=True)
        box.addWidget(self.summary)
        self.table = table(["Session", "Imported", "Mode", "Sidebar rows", "Status"], 0)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.itemSelectionChanged.connect(self._selected)
        box.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.check_button = QPushButton("Check imports")
        self.check_button.clicked.connect(self.check)
        self.show_button = QPushButton("Show in Sessions")
        self.show_button.clicked.connect(self._show)
        self.remove_button = QPushButton("Remove selected")
        self.remove_button.clicked.connect(self._remove)
        row.addWidget(self.check_button)
        row.addStretch(1)
        row.addWidget(self.show_button)
        row.addWidget(self.remove_button)
        box.addLayout(row)
        self._selected()

    def check(self) -> None:
        self.check_button.setEnabled(False)
        self.summary.setText("Checking imports…")
        run_task(self.bridge.maintenance.imports, on_done=self._filled,
                 on_failed=lambda message, _tb: self._failed(message), owner=self)

    def _failed(self, message: str) -> None:
        self.check_button.setEnabled(True)
        self.summary.setText(f"The check failed: {message}")

    def _filled(self, records: list[ImportRecord]) -> None:
        c = colors()
        self.records = records
        self.check_button.setEnabled(True)
        self.table.setRowCount(len(records))
        for i, r in enumerate(records):
            self.table.setItem(i, 0, cell(r.title[:70], data=r, tip=f"{r.claude_id}\n{r.transcript}"))
            self.table.setItem(i, 1, cell(r.imported_at[:16].replace("T", " ") or "Unknown"))
            self.table.setItem(i, 2, cell(r.mode or "Unknown"))
            self.table.setItem(i, 3, cell(str(len(r.cards))))
            protected = self.bridge.maintenance.removal_block(r)
            text = " ".join(r.problems) or "Intact"
            if protected:
                text = f"Protected. {text}" if r.problems else f"Protected. {protected}"
            self.table.setItem(i, 4, cell(text, c.warn if r.problems else c.ok))
        healthy = sum(r.healthy for r in records)
        self.summary.setText(f"{len(records)} import(s): {healthy} intact, "
                             f"{len(records) - healthy} needing attention.")
        self._selected()

    def _selection(self) -> list[ImportRecord]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [self.table.item(r, 0).data(Qt.UserRole) for r in rows]

    def _selected(self) -> None:
        chosen = self._selection()
        self.remove_button.setEnabled(bool(chosen))
        self.show_button.setEnabled(len(chosen) == 1 and bool(chosen[0].thread_id))

    def _show(self) -> None:
        chosen = self._selection()
        if chosen and chosen[0].thread_id:
            self.show_session.emit(("codex", chosen[0].thread_id))

    def _remove(self) -> None:
        chosen = self._selection()
        if not chosen:
            return
        self.remove_button.setEnabled(False)
        run_task(self.bridge.desktop_running, on_done=lambda running: self._confirm(chosen, running),
                 on_failed=lambda _m, _tb: self._confirm(chosen, False), owner=self)

    def _confirm(self, chosen: list[ImportRecord], running: bool) -> None:
        answer = QMessageBox.question(
            self, "Remove these imports?",
            f"{len(chosen)} import(s) move to the Archived tab, with their sidebar rows and ledger "
            f"entries. You can restore them there. Protected or in-use sessions are skipped."
            f"{_relaunch(running)}")
        if answer != QMessageBox.Yes:
            self._selected()
            return
        run_task(lambda: self.bridge.remove_imports(chosen), on_done=self._removed,
                 on_failed=lambda message, _tb: self._not_done(message), owner=self)

    def _not_done(self, message: str) -> None:
        QMessageBox.warning(self, "Not done", message)
        self._selected()

    def _removed(self, result) -> None:
        lines = [f"Moved to the archive: {len(result.moved)}."]
        if result.record_only:
            lines.append(f"Record removed only: {len(result.record_only)}. These had no transcript "
                         "to move, or are native Claude sessions, which stay.")
        lines += [f"Skipped {title}: {why}" for title, why in result.refused]
        QMessageBox.information(self, "Done", "\n".join(lines))
        self.changed.emit()
        self.check()


class BulkPanel(QWidget):
    """Import many Codex threads in one go, each through the same checks as a single handoff."""
    changed = Signal()

    def __init__(self, bridge: ContextBridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.snapshot = Snapshot()
        self._running = False
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 8, 0, 0)
        box.addWidget(label("New Codex threads get a new Claude session, and threads already in "
                            "Claude get only their new turns. Diverged threads stay unticked: ticking "
                            "one merges its new turns into the Claude session. To keep them apart, "
                            "use Send to Claude for a separate continuation. Each thread is "
                            "rechecked when its turn comes, so ones in use are skipped.",
                            "muted", wrap=True))
        self.table = table(["Session", "Project", "State", "What happens"], 0)
        box.addWidget(self.table, 1)
        form = QFormLayout()
        self.mode = QComboBox()
        self.mode.addItem("Faithful: real tool calls", "faithful")
        self.mode.addItem("Archive: tool activity as readable text", "archive")
        form.addRow(label("Conversion"), self.mode)
        self.budget = QComboBox()
        for text, value in BUDGETS:
            if value != -1:
                self.budget.addItem(text, value)
        form.addRow(label("Context budget"), self.budget)
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("Optional, for example [codex]")
        form.addRow(label("Title prefix"), self.prefix)
        self.desktop = QCheckBox("Also add rows to Claude Desktop's sidebar")
        form.addRow(label(""), self.desktop)
        box.addLayout(form)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = label("", "muted", wrap=True)
        box.addWidget(self.progress)
        box.addWidget(self.status)
        row = QHBoxLayout()
        self.import_button = QPushButton("Import checked")
        self.import_button.clicked.connect(self._start)
        row.addStretch(1)
        row.addWidget(self.import_button)
        box.addLayout(row)
        self._load_defaults()

    def _load_defaults(self) -> None:
        saved = saved_options()
        self.mode.setCurrentIndex(max(0, self.mode.findData(saved.mode)))
        self.budget.setCurrentIndex(max(0, self.budget.findData(saved.context_budget)))

    def fill(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        if self._running:
            return
        rows = self.bridge.bulk_rows(snapshot)
        self.table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            s = row.session
            first = cell(s.title[:70], data=s, tip=s.session_id)
            flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
            if row.selectable:
                flags |= Qt.ItemIsUserCheckable
                first.setCheckState(Qt.Checked if row.checked else Qt.Unchecked)
            first.setFlags(flags)
            self.table.setItem(i, 0, first)
            self.table.setItem(i, 1, cell(s.project or s.cwd))
            self.table.setItem(i, 2, cell(s.state.label))
            self.table.setItem(i, 3, cell(row.action, None if row.selectable else colors().muted))
        self.import_button.setEnabled(bool(rows))

    def _checked(self) -> list:
        out = []
        for i in range(self.table.rowCount()):
            item: QTableWidgetItem = self.table.item(i, 0)
            if item.flags() & Qt.ItemIsUserCheckable and item.checkState() == Qt.Checked:
                out.append(item.data(Qt.UserRole))
        return out

    def options(self) -> HandoffOptions:
        return replace(saved_options(), mode=self.mode.currentData(),
                       context_budget=self.budget.currentData(),
                       title_prefix=self.prefix.text(), register_desktop=self.desktop.isChecked())

    def _start(self) -> None:
        sessions = self._checked()
        if not sessions:
            QMessageBox.information(self, "Nothing checked", "Check at least one thread to import.")
            return
        merges = sum(s.state is SyncState.BOTH_ADVANCED for s in sessions)
        merging = (f"\n\n{merges} of them have diverged. Their new Codex turns merge into a Claude "
                   "session that has its own newer work, and Claude will read both branches as one "
                   "conversation." if merges else "")
        answer = QMessageBox.question(
            self, "Import these threads?",
            f"{len(sessions)} Codex thread(s) go to Claude. Existing transcripts are backed up "
            f"before new turns are added, and Codex's files aren't changed.{merging}")
        if answer != QMessageBox.Yes:
            return
        options = self.options()
        self._running = True
        self.import_button.setEnabled(False)
        self.progress.setRange(0, len(sessions))
        self.progress.setValue(0)
        self.progress.setVisible(True)
        run_task(lambda say: self.bridge.bulk_import(sessions, options, say), on_done=self._done,
                 on_failed=lambda message, _tb: self._failed(message), on_progress=self._step, owner=self)

    def _step(self, message: str) -> None:
        self.status.setText(message)
        if message.startswith("Importing "):
            self.progress.setValue(self.progress.value() + 1)

    def _failed(self, message: str) -> None:
        self._running = False
        self.progress.setVisible(False)
        self.import_button.setEnabled(True)
        QMessageBox.warning(self, "Not done", message)

    def _done(self, results: list) -> None:
        self._running = False
        self.progress.setVisible(False)
        self.status.setText("")
        good = [r for r in results if r.status == "committed"]
        lines = [f"Imported: {len(good)} of {len(results)}."]
        lines += [f"{r.session.title[:50]}: {r.detail}" for r in results if r.status != "committed"]
        QMessageBox.information(self, "Done", "\n".join(lines))
        self.changed.emit()


class DesktopPanel(QWidget):
    """Claude Desktop's sidebar: rows with no transcript, and rows in another account."""
    changed = Signal()

    def __init__(self, bridge: ContextBridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.dead = []
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 8, 0, 0)
        box.addWidget(label("ROWS THAT WON'T OPEN", "section"))
        box.addWidget(label("These sidebar rows point to transcripts that no longer exist, so they "
                            "open on \"session not found\". Clearing moves them to ContextBridge's "
                            "archive.", "muted", wrap=True))
        self.dead_table = table(["Row", "Account"], 0)
        box.addWidget(self.dead_table, 1)
        row = QHBoxLayout()
        self.refresh_button = QPushButton("Check Claude Desktop")
        self.refresh_button.clicked.connect(self.check)
        self.restore_button = QPushButton("Put cleared rows back")
        self.restore_button.clicked.connect(self._restore)
        self.clear_button = QPushButton("Clear rows")
        self.clear_button.clicked.connect(self._clear)
        row.addWidget(self.refresh_button)
        row.addStretch(1)
        row.addWidget(self.restore_button)
        row.addWidget(self.clear_button)
        box.addLayout(row)

        box.addWidget(label("MOVE SESSIONS BETWEEN ACCOUNTS", "section"))
        box.addWidget(label("Transcripts aren't tied to an account, but sidebar rows are, so signing "
                            "in to another account empties the sidebar. This copies the rows across. "
                            "Rows already there are skipped, so it is safe to repeat.",
                            "muted", wrap=True))
        form = QFormLayout()
        self.source = QComboBox()
        self.destination = QComboBox()
        form.addRow(label("From"), self.source)
        form.addRow(label("To"), self.destination)
        box.addLayout(form)
        self.remove_source = QCheckBox("Take the rows out of the old account (they move to the archive)")
        box.addWidget(self.remove_source)
        row = QHBoxLayout()
        self.move_status = label("", "muted", wrap=True)
        self.move_button = QPushButton("Copy rows")
        self.move_button.clicked.connect(self._move)
        row.addWidget(self.move_status, 1)
        row.addWidget(self.move_button)
        box.addLayout(row)
        self.source.currentIndexChanged.connect(self._sync_buttons)
        self.destination.currentIndexChanged.connect(self._sync_buttons)
        self._sync_buttons()

    def check(self) -> None:
        self.refresh_button.setEnabled(False)
        tools = self.bridge.maintenance
        run_task(lambda: (tools.dead_cards(), tools.accounts(), len(tools.archived_cards())),
                 on_done=self._filled, on_failed=lambda message, _tb: self._failed(message), owner=self)

    def _failed(self, message: str) -> None:
        self.refresh_button.setEnabled(True)
        QMessageBox.warning(self, "Not done", message)

    def _filled(self, result) -> None:
        self.dead, accounts, archived = result
        self.refresh_button.setEnabled(True)
        self.dead_table.setRowCount(len(self.dead))
        for i, card in enumerate(self.dead):
            self.dead_table.setItem(i, 0, cell(card.title[:70], tip=str(card.path)))
            self.dead_table.setItem(i, 1, cell(card.account))
        self.clear_button.setEnabled(bool(self.dead))
        self.restore_button.setEnabled(archived > 0)
        for combo in (self.source, self.destination):
            combo.blockSignals(True)
            combo.clear()
            for account in accounts:
                now = " (signed in now)" if account.current else ""
                combo.addItem(f"{account.label}, {account.cards} row(s){now}", account.path)
            combo.blockSignals(False)
        current = next((i for i, a in enumerate(accounts) if a.current), 0)
        other = next((i for i in range(len(accounts)) if i != current), current)
        self.destination.setCurrentIndex(current)
        self.source.setCurrentIndex(other)
        self._sync_buttons()

    def _sync_buttons(self, *_args) -> None:
        src, dst = self.source.currentData(), self.destination.currentData()
        self.move_button.setEnabled(bool(src and dst and src != dst))
        if not self.dead:
            self.clear_button.setEnabled(False)

    def _clear(self) -> None:
        cards = self.dead
        if not cards:
            return
        self.clear_button.setEnabled(False)
        run_task(self.bridge.desktop_running, on_done=lambda running: self._confirm_clear(cards, running),
                 on_failed=lambda _m, _tb: self._confirm_clear(cards, False), owner=self)

    def _confirm_clear(self, cards: list, running: bool) -> None:
        answer = QMessageBox.question(self, "Clear these rows?",
                                      f"{len(cards)} sidebar row(s) move to ContextBridge's archive. "
                                      f"Put cleared rows back undoes this.{_relaunch(running)}")
        if answer != QMessageBox.Yes:
            self.clear_button.setEnabled(True)
            return
        run_task(lambda: self.bridge.maintenance.clear_dead_cards(cards),
                 on_done=lambda n: self._acted(f"Cleared {n} row(s)."),
                 on_failed=lambda message, _tb: self._failed(message), owner=self)

    def _restore(self) -> None:
        run_task(self.bridge.maintenance.restore_cards,
                 on_done=lambda n: self._acted(f"Put back {n} row(s)."),
                 on_failed=lambda message, _tb: self._failed(message), owner=self)

    def _move(self) -> None:
        src, dst = self.source.currentData(), self.destination.currentData()
        if not src or not dst or src == dst:
            return
        self.move_button.setEnabled(False)
        tools = self.bridge.maintenance
        run_task(lambda: (tools.plan_move(src, dst), self.bridge.desktop_running()),
                 on_done=lambda result: self._confirm_move(src, dst, *result),
                 on_failed=lambda message, _tb: self._failed(message), owner=self)

    def _confirm_move(self, src, dst, rows: list, running: bool) -> None:
        copy = sum(r.action == "copy" for r in rows)
        taken = self.remove_source.isChecked()
        answer = QMessageBox.question(
            self, "Copy these rows?",
            f"{copy} row(s) will be copied. {sum(r.action == 'already_there' for r in rows)} are "
            f"already there, and {sum(r.action == 'no_transcript' for r in rows)} have no transcript "
            f"on this PC.{' The originals move to the archive.' if taken else ''}{_relaunch(running)}")
        if answer != QMessageBox.Yes:
            self._sync_buttons()
            return
        run_task(lambda: self.bridge.maintenance.move_cards(src, dst, remove_source=taken),
                 on_done=lambda r: self._acted(
                     f"Copied {r['copied']}. {r['already_there']} already there, "
                     f"{r['no_transcript']} without a transcript."),
                 on_failed=lambda message, _tb: self._failed(message), owner=self)

    def _acted(self, message: str) -> None:
        QMessageBox.information(self, "Done", message)
        self.changed.emit()
        self.check()


__all__ = ["BulkPanel", "DesktopPanel", "ImportsPanel"]
