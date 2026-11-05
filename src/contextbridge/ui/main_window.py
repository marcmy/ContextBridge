"""The main window."""
from __future__ import annotations

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMainWindow, QProgressBar, QPushButton, QSplitter, QStackedWidget,
                               QVBoxLayout, QWidget)

from .. import APP_NAME, TAGLINE, __version__
from ..models import Provider, Session
from ..service import ContextBridge, Snapshot
from .details import DetailsPanel, label
from .handoff_dialog import HandoffDialog
from .manage import ManagePage
from .session_list import STATUS_FILTERS, SessionTree
from .workers import run_task


class MainWindow(QMainWindow):
    def __init__(self, bridge: ContextBridge):
        super().__init__()
        self.bridge = bridge
        self.snapshot = Snapshot()
        self._refreshing = False
        self._refresh_again = False
        self._sized = False
        self.setWindowTitle(APP_NAME)
        self.resize(1320, 820)
        self.setMinimumSize(960, 600)

        root = QWidget()
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_nav())
        self.pages = QStackedWidget()
        self.pages.addWidget(self._build_sessions())
        self.manage = ManagePage(bridge)
        self.manage.changed.connect(self.refresh)
        self.manage.show_session.connect(self._reveal)
        self.pages.addWidget(self._wrap_manage())
        layout.addWidget(self.pages, 1)
        self.setCentralWidget(root)

        status = self.statusBar()
        self.busy = QProgressBar()
        self.busy.setRange(0, 0)
        self.busy.setFixedWidth(120)
        self.busy.setFixedHeight(6)
        self.busy.setTextVisible(False)
        self.busy.setVisible(False)
        self.status_text = QLabel("")
        self.status_text.setContentsMargins(10, 0, 0, 0)
        status.addWidget(self.status_text, 1)
        status.addPermanentWidget(self.busy)

        QShortcut(QKeySequence.Find, self, activated=lambda: self.search.setFocus())
        QShortcut(QKeySequence.Refresh, self, activated=self.refresh)
        QTimer.singleShot(0, self.refresh)

    # -- construction ----------------------------------------------------------------------
    def _build_nav(self) -> QWidget:
        rail = QFrame()
        rail.setObjectName("navRail")
        rail.setFixedWidth(176)
        box = QVBoxLayout(rail)
        box.setContentsMargins(12, 14, 12, 12)
        box.setSpacing(4)
        box.addWidget(label(APP_NAME, "h2"))
        box.addWidget(label(TAGLINE, "faint", wrap=True))
        box.addSpacing(14)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        for index, text in enumerate(("Sessions", "Imports and cleanup")):
            button = QPushButton(text)
            button.setProperty("role", "nav")
            button.setCheckable(True)
            button.setChecked(index == 0)
            self.nav_group.addButton(button, index)
            box.addWidget(button)
        self.nav_group.idClicked.connect(lambda i: self.pages.setCurrentIndex(i))
        box.addStretch(1)
        box.addWidget(label(f"Version {__version__}", "faint"))
        return rail

    def _build_sessions(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(14, 12, 14, 8)
        box.setSpacing(8)
        self.problem_banner = QFrame()
        self.problem_banner.setProperty("role", "banner-danger")
        banner_box = QVBoxLayout(self.problem_banner)
        banner_box.setContentsMargins(10, 7, 10, 7)
        self.problem_text = label("", wrap=True)
        banner_box.addWidget(self.problem_text)
        self.problem_banner.setVisible(False)
        box.addWidget(self.problem_banner)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, session ID, folder or branch")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._filters_changed)
        bar.addWidget(self.search, 1)
        self.provider_filter = QComboBox()
        self.provider_filter.addItem("All agents", None)
        self.provider_filter.addItem("Codex", "codex")
        self.provider_filter.addItem("Claude", "claude")
        self.provider_filter.currentIndexChanged.connect(self._filters_changed)
        bar.addWidget(self.provider_filter)
        self.status_filter = QComboBox()
        for text, states in STATUS_FILTERS:
            self.status_filter.addItem(text, states)
        self.status_filter.currentIndexChanged.connect(self._filters_changed)
        bar.addWidget(self.status_filter)
        self.show_archived = QCheckBox("Archived")
        self.show_archived.toggled.connect(self._filters_changed)
        bar.addWidget(self.show_archived)
        self.show_internal = QCheckBox("Internal agents")
        self.show_internal.setToolTip("Codex guardians, subagents and similar threads no person started")
        self.show_internal.toggled.connect(self._internal_toggled)
        bar.addWidget(self.show_internal)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        bar.addWidget(self.refresh_button)
        box.addLayout(bar)

        split = QSplitter(Qt.Horizontal)
        self.tree = SessionTree()
        self.tree.selectionModel().currentChanged.connect(self._selection_changed)
        self.empty_hint = label("", "muted")
        split.addWidget(self.tree)
        self.details = DetailsPanel(self.bridge)
        self.details.send_requested.connect(self.open_handoff)
        split.addWidget(self.details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setCollapsible(0, False)
        split.setCollapsible(1, False)
        self.split = split
        box.addWidget(split, 1)
        return page

    def _wrap_manage(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        box.setContentsMargins(14, 12, 14, 8)
        box.addWidget(label("Imports and cleanup", "h1"))
        box.addWidget(self.manage, 1)
        return page

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._sized:
            self._sized = True
            width = self.split.width() or 1100
            self.split.setSizes([int(width * 0.6), int(width * 0.4)])

    # -- data ----------------------------------------------------------------------------
    def refresh(self) -> None:
        if self._refreshing:
            self._refresh_again = True
            return
        self._refreshing = True
        self.busy.setVisible(True)
        self.refresh_button.setEnabled(False)
        include_internal = self.show_internal.isChecked()
        run_task(lambda say: self.bridge.refresh(progress=say, include_internal=include_internal),
                 on_progress=self.status_text.setText, on_done=self._refreshed,
                 on_failed=self._refresh_failed, owner=self)

    def _refreshed(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self._refreshing = False
        self.busy.setVisible(False)
        self.refresh_button.setEnabled(True)
        self.tree.populate(snapshot.sessions)
        self.tree.apply_filter(**self._filter_values())
        self.manage.fill(snapshot)
        self.problem_banner.setVisible(bool(snapshot.problems))
        self.problem_text.setText("\n".join(snapshot.problems))
        self._update_details()
        self._update_status()
        if self._refresh_again:
            self._refresh_again = False
            self.refresh()

    def _refresh_failed(self, message: str, _trace: str) -> None:
        self._refreshing = False
        self.busy.setVisible(False)
        self.refresh_button.setEnabled(True)
        self.status_text.setText(f"Could not read sessions: {message}")

    def _update_status(self) -> None:
        sessions = self.snapshot.sessions
        codex = sum(1 for s in sessions if s.provider is Provider.CODEX)
        visible = self.tree.visible_count()
        projects = len({s.project or s.cwd for s in sessions})
        shown = "" if visible == len(sessions) else f"{visible} shown of "
        self.status_text.setText(f"{shown}{len(sessions)} conversations in {projects} projects: "
                                 f"{codex} Codex, {len(sessions) - codex} Claude")

    def _filter_values(self) -> dict:
        return dict(search=self.search.text().strip(), provider=self.provider_filter.currentData(),
                    states=self.status_filter.currentData(), show_archived=self.show_archived.isChecked(),
                    show_internal=self.show_internal.isChecked())

    def _filters_changed(self, *_args) -> None:
        self.tree.apply_filter(**self._filter_values())
        self._update_status()

    def _internal_toggled(self, *_args) -> None:
        self.refresh()

    def _selection_changed(self, *_args) -> None:
        self._update_details()

    def _update_details(self) -> None:
        self.details.show_session(self.tree.current_session())

    def _reveal(self, key: tuple[str, str]) -> None:
        self.nav_group.button(0).setChecked(True)
        self.pages.setCurrentIndex(0)
        self.search.clear()
        self.status_filter.setCurrentIndex(0)
        self.provider_filter.setCurrentIndex(0)
        self.tree.select(key)

    # -- handoff -----------------------------------------------------------------------
    def open_handoff(self, session: Session, target: Provider) -> None:
        dialog = HandoffDialog(self.bridge, session, target, self)
        dialog.transfer_finished.connect(lambda _report: self.refresh())
        dialog.exec()


def app_icon() -> QIcon:
    from pathlib import Path
    return QIcon(str(Path(__file__).resolve().parent / "assets" / "icon.svg"))


__all__ = ["MainWindow", "app_icon", "QSettings"]
