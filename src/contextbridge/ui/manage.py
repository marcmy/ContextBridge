"""Import management: past transfers, duplicates and safe cleanup."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget

from ..models import DuplicateCandidate, Provider
from ..service import CleanupRefused, ContextBridge, Snapshot
from .details import label
from .maintenance import BulkPanel, DesktopPanel, ImportsPanel
from .manage_table import cell, table
from .theme import colors, provider_color
from .workers import run_task

STATUS_TEXT = {"committed": "Completed", "failed": "Failed", "cancelled": "Cancelled",
               "writing": "In progress", "interrupted": "Interrupted",
               "assisted_pending": "Waiting on Codex"}
STRATEGY_TEXT = {"new_session": "New session", "append": "Append new turns", "merge": "Merge diverged turns", "replace": "Rebuild in place",
                 "continuation": "Separate continuation", "native_import": "Codex importer",
                 "native_refresh": "Codex importer update", "delta_import": "Handoff (new work only)",
                 "delta_full": "Whole session", "delta_refresh": "Handoff update"}
KIND_TEXT = {"circular_copy": "Circular re-import", "repeat_import": "Imported twice",
             "content_overlap": "Same opening turns", "internal_import": "Import of an internal agent"}


class ManagePage(QWidget):
    changed = Signal()
    show_session = Signal(object)

    def __init__(self, bridge: ContextBridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.snapshot = Snapshot()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        # Transfers
        transfers = QWidget()
        box = QVBoxLayout(transfers)
        box.setContentsMargins(0, 8, 0, 0)
        self.transfer_table = table(["When", "Status", "From", "To", "Type", "Detail"], 5)
        box.addWidget(self.transfer_table, 1)
        row = QHBoxLayout()
        self.open_report = QPushButton("Open report")
        self.open_report.clicked.connect(self._open_report)
        row.addStretch(1)
        row.addWidget(self.open_report)
        box.addLayout(row)
        self.tabs.addTab(transfers, "Transfers")
        self.transfer_table.itemDoubleClicked.connect(lambda _item: self._open_report())

        # Duplicates
        duplicates = QWidget()
        box = QVBoxLayout(duplicates)
        box.setContentsMargins(0, 8, 0, 0)
        box.addWidget(label("Found from recorded provenance. \"Same opening turns\" is only a lead: "
                            "it is never offered for cleanup, and nothing here is removed "
                            "automatically.", "muted", wrap=True))
        self.duplicate_table = table(["Session", "Agent", "Finding", "Evidence", "Can archive", "Why"], 5)
        box.addWidget(self.duplicate_table, 1)
        row = QHBoxLayout()
        self.show_button = QPushButton("Show in Sessions")
        self.show_button.clicked.connect(self._show_selected)
        self.archive_button = QPushButton("Archive")
        self.archive_button.setToolTip("Reversible. Codex keeps the thread under its archive; a "
                                       "Claude transcript moves to ContextBridge's archive folder.")
        self.archive_button.clicked.connect(self._archive_selected)
        row.addStretch(1)
        row.addWidget(self.show_button)
        row.addWidget(self.archive_button)
        box.addLayout(row)
        self.duplicate_table.itemSelectionChanged.connect(self._duplicate_selected)
        self.tabs.addTab(duplicates, "Duplicates")

        # Archived
        archived = QWidget()
        box = QVBoxLayout(archived)
        box.setContentsMargins(0, 8, 0, 0)
        self.archive_table = table(["Archived", "Agent", "Session", "Where"], 3)
        box.addWidget(self.archive_table, 1)
        row = QHBoxLayout()
        self.restore_button = QPushButton("Restore")
        self.restore_button.clicked.connect(self._restore_selected)
        row.addStretch(1)
        row.addWidget(self.restore_button)
        box.addLayout(row)
        self.tabs.addTab(archived, "Archived")

        self.imports = ImportsPanel(bridge)
        self.imports.changed.connect(self.changed)
        self.imports.show_session.connect(self.show_session)
        self.tabs.addTab(self.imports, "Import health")
        self.bulk = BulkPanel(bridge)
        self.bulk.changed.connect(self.changed)
        self.tabs.addTab(self.bulk, "Bulk import")
        self.desktop = DesktopPanel(bridge)
        self.desktop.changed.connect(self.changed)
        self.tabs.addTab(self.desktop, "Claude Desktop")
        self.tabs.currentChanged.connect(self._tab_opened)
        self._opened: set[QWidget] = set()

    def fill(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self._fill_transfers(snapshot.transfers)
        self._fill_duplicates(snapshot.duplicates)
        self._fill_archived(snapshot.archived)
        self.bulk.fill(snapshot)
        count = len(snapshot.duplicates)
        self.tabs.setTabText(1, f"Duplicates ({count})" if count else "Duplicates")
        self.tabs.setTabText(2, f"Archived ({len(snapshot.archived)})" if snapshot.archived else "Archived")

    def _tab_opened(self, _index: int) -> None:
        """These read many files, so they run the first time their tab is opened."""
        page = self.tabs.currentWidget()
        if page in (self.imports, self.desktop) and page not in self._opened:
            self._opened.add(page)
            page.check()

    def _fill_transfers(self, rows: list[dict]) -> None:
        c = colors()
        self.transfer_table.setRowCount(len(rows))
        for i, t in enumerate(rows):
            status = t["status"]
            color = {"committed": c.ok, "failed": c.danger, "interrupted": c.danger,
                     "assisted_pending": c.warn}.get(status, c.muted)
            self.transfer_table.setItem(i, 0, cell((t["started_at"] or "")[:16].replace("T", " "),
                                                   data=t.get("report_path")))
            self.transfer_table.setItem(i, 1, cell(STATUS_TEXT.get(status, status), color))
            self.transfer_table.setItem(i, 2, cell(f"{t['src_provider'].title()} {t['src_id'][:13]}",
                                                   provider_color(t["src_provider"]), t["src_id"]))
            dst = t.get("dst_id") or ""
            self.transfer_table.setItem(i, 3, cell(f"{t['dst_provider'].title()} {dst[:13]}".strip(),
                                                   provider_color(t["dst_provider"]), dst))
            self.transfer_table.setItem(i, 4, cell(STRATEGY_TEXT.get(t["strategy"], t["strategy"])))
            self.transfer_table.setItem(i, 5, cell((t.get("error") or "").split("\n")[0],
                                                   c.danger if t.get("error") else None, t.get("error") or ""))

    def _fill_duplicates(self, rows: list[DuplicateCandidate]) -> None:
        c = colors()
        order = sorted(rows, key=lambda d: (not d.cleanup_allowed, d.certainty != "provenance"))
        self.duplicate_table.setRowCount(len(order))
        for i, d in enumerate(order):
            s = self.snapshot.by_key.get(d.session)
            title = (s.title if s else d.session[1])[:60]
            self.duplicate_table.setItem(i, 0, cell(title, data=d, tip=d.session[1]))
            self.duplicate_table.setItem(i, 1, cell(d.session[0].title(), provider_color(d.session[0])))
            self.duplicate_table.setItem(i, 2, cell(KIND_TEXT.get(d.kind, d.kind)))
            self.duplicate_table.setItem(i, 3, cell("Recorded" if d.certainty == "provenance" else "Possible only",
                                                    c.muted if d.certainty == "provenance" else c.warn))
            yes = d.cleanup_allowed
            self.duplicate_table.setItem(i, 4, cell("Yes" if yes else "Protected" if d.has_native_work else "No",
                                                    c.ok if yes else c.muted))
            self.duplicate_table.setItem(i, 5, cell(d.reason))
        self._duplicate_selected()

    def _fill_archived(self, rows: list[dict]) -> None:
        self.archive_table.setRowCount(len(rows))
        for i, a in enumerate(rows):
            self.archive_table.setItem(i, 0, cell(a["archived_at"][:16].replace("T", " "), data=a))
            self.archive_table.setItem(i, 1, cell(a["provider"].title(), provider_color(a["provider"])))
            self.archive_table.setItem(i, 2, cell(a["session_id"]))
            self.archive_table.setItem(i, 3, cell(a["archive_path"] or "Codex archive"))

    def _selected_duplicate(self) -> DuplicateCandidate | None:
        row = self.duplicate_table.currentRow()
        item = self.duplicate_table.item(row, 0) if row >= 0 else None
        return item.data(Qt.UserRole) if item else None

    def _duplicate_selected(self) -> None:
        d = self._selected_duplicate()
        self.show_button.setEnabled(d is not None)
        self.archive_button.setEnabled(d is not None and d.cleanup_allowed)

    def _show_selected(self) -> None:
        d = self._selected_duplicate()
        if d is not None:
            self.show_session.emit(d.session)

    def _archive_selected(self) -> None:
        d = self._selected_duplicate()
        s = self.snapshot.by_key.get(d.session) if d else None
        if d is None or s is None or not d.cleanup_allowed:
            return
        where = ("Codex moves the thread to its archive. You can restore it here or with "
                 "`codex unarchive`." if s.provider is Provider.CODEX else
                 "The transcript moves to ContextBridge's archive folder. Restoring puts it back.")
        answer = QMessageBox.question(self, "Archive this session?",
                                      f"{s.title}\n\n{d.reason}\n\n{where}\n\nNothing is deleted.")
        if answer != QMessageBox.Yes:
            return
        self.archive_button.setEnabled(False)
        run_task(lambda: self.bridge.archive(s), on_done=lambda message: self._acted(message),
                 on_failed=lambda message, _tb: self._refused(message), owner=self)

    def _restore_selected(self) -> None:
        row = self.archive_table.currentRow()
        item = self.archive_table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        entry = item.data(Qt.UserRole)
        run_task(lambda: self.bridge.restore(entry["provider"], entry["session_id"]),
                 on_done=lambda message: self._acted(message),
                 on_failed=lambda message, _tb: self._refused(message), owner=self)

    def _acted(self, message: str) -> None:
        QMessageBox.information(self, "Done", message)
        self.changed.emit()

    def _refused(self, message: str) -> None:
        QMessageBox.warning(self, "Not done", message)
        self._duplicate_selected()

    def _open_report(self) -> None:
        row = self.transfer_table.currentRow()
        item = self.transfer_table.item(row, 0) if row >= 0 else None
        path = item.data(Qt.UserRole) if item else None
        if path and Path(path).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


__all__ = ["CleanupRefused", "ManagePage"]
