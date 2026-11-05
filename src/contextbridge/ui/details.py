"""The selected session: what it is, where it came from, and what state its workspace is in."""
from __future__ import annotations

from datetime import datetime
from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from ..models import Provider, Session
from ..service import ContextBridge, open_in_terminal, resume_text
from .theme import colors, provider_color, severity_color
from .workers import run_task


def _short_path(path: str, limit: int = 58) -> str:
    """A long unbroken path can't wrap in a label; the tooltip keeps the full one."""
    return path if len(path) <= limit else f"{path[:20]}…{path[-(limit - 21):]}"


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M") if ms else "unknown"


def label(text: str = "", role: str | None = None, wrap: bool = False, select: bool = False) -> QLabel:
    widget = QLabel(text)
    if role:
        widget.setProperty("role", role)
    widget.setWordWrap(wrap)
    if select:
        widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return widget


def scroll(body: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidget(body)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    return area


def pill_html(text: str, color: str) -> str:
    return f'<span style="color:{color}; font-weight:600;">{escape(text)}</span>'


class FactGrid(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setHorizontalSpacing(14)
        self.grid.setVerticalSpacing(7)
        self.grid.setColumnStretch(1, 1)

    def fill(self, rows: list[tuple[str, str]]) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for i, (name, value) in enumerate(rows):
            key = label(name, "muted")
            key.setAlignment(Qt.AlignTop | Qt.AlignLeft)
            self.grid.addWidget(key, i, 0)
            self.grid.addWidget(label(value, wrap=True, select=True), i, 1)


class LineageView(QWidget):
    """The session's logical conversation as a chain of transfers."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 8, 0, 8)
        self.box.setSpacing(6)

    def fill(self, current: Session, chain: list[dict]) -> None:
        while self.box.count():
            item = self.box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not chain:
            self.box.addWidget(label(
                "No recorded relationship to another session. A title or folder match is not "
                "treated as one.", "muted", wrap=True))
            self.box.addStretch(1)
            return
        for entry in chain:
            link = entry["link"]
            card = QFrame()
            card.setProperty("role", "card")
            inner = QVBoxLayout(card)
            inner.setContentsMargins(10, 8, 10, 8)
            inner.setSpacing(3)
            head = QLabel(f"{self._node(link.src_provider, link.src_id, entry['src'], current)}"
                          f"  →  {self._node(link.dst_provider, link.dst_id, entry['dst'], current)}")
            head.setTextFormat(Qt.RichText)
            head.setWordWrap(True)
            inner.addWidget(head)
            when = (link.transferred_at or "")[:16].replace("T", " ")
            how = {"import": "Imported", "native_import": "Codex importer", "round-trip": "Synced back",
                   "delta_import": "Handoff (new work only)", "continuation": "Separate continuation"
                   }.get(link.relation, link.relation)
            origin = {"contextbridge": "ContextBridge", "claude-ledger": "Claude import ledger",
                      "codex-ledger": "Codex import ledger", "transcript-marker": "transcript provenance"
                      }.get(link.origin, link.origin)
            inner.addWidget(label(f"{how} · {when} · recorded by {origin}", "faint", wrap=True))
            if link.bounded:
                inner.addWidget(label("Context-bounded: the destination holds a partial history.", "faint"))
            self.box.addWidget(card)
        self.box.addStretch(1)

    def _node(self, provider: Provider, sid: str, session: Session | None, current: Session) -> str:
        color = provider_color(provider.value)
        name = f"{provider.label} {sid[:13] if provider is Provider.CODEX else sid[:8]}"
        text = pill_html(name, color)
        if session is None:
            text += ' <span style="color:%s;">(not found)</span>' % colors().warn
        elif session.key == current.key:
            text += " (this)"
        return text


class DetailsPanel(QFrame):
    send_requested = Signal(object, object)     # session, target provider

    def __init__(self, bridge: ContextBridge, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.session: Session | None = None
        self.setProperty("role", "panel")
        self.setMinimumWidth(400)
        self.stack = QStackedWidget()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.stack)
        empty = label("Select a conversation to see its lineage and state.", "muted")
        empty.setAlignment(Qt.AlignCenter)
        self.stack.addWidget(empty)
        self.stack.addWidget(self._build())

    def _build(self) -> QWidget:
        body = QWidget()
        box = QVBoxLayout(body)
        box.setContentsMargins(16, 14, 16, 12)
        box.setSpacing(8)
        self.title = label("", "h2", wrap=True, select=True)
        self.badge = QLabel()
        self.badge.setTextFormat(Qt.RichText)
        self.detail = label("", "muted", wrap=True)
        box.addWidget(self.title)
        box.addWidget(self.badge)
        box.addWidget(self.detail)

        self.warning_box = QFrame()
        self.warning_box.setProperty("role", "banner-warn")
        warn_layout = QVBoxLayout(self.warning_box)
        warn_layout.setContentsMargins(10, 7, 10, 7)
        self.warning_text = label("", wrap=True)
        warn_layout.addWidget(self.warning_text)
        box.addWidget(self.warning_box)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.send = QPushButton("Send to Claude")
        self.send.setProperty("role", "primary")
        self.send.clicked.connect(self._send)
        self.open_here = QPushButton("Resume")
        self.open_here.setToolTip("Open a terminal running this session's resume command")
        self.open_here.clicked.connect(self._open)
        self.copy = QPushButton("Copy command")
        self.copy.setToolTip("Copy this session's resume command")
        self.copy.clicked.connect(self._copy)
        actions.addWidget(self.send)
        actions.addWidget(self.open_here)
        actions.addWidget(self.copy)
        actions.addStretch(1)
        box.addLayout(actions)

        self.tabs = QTabWidget()
        self.overview = FactGrid()
        overview_wrap = QWidget()
        wrap_box = QVBoxLayout(overview_wrap)
        wrap_box.setContentsMargins(0, 8, 0, 8)
        wrap_box.addWidget(self.overview)
        wrap_box.addStretch(1)
        self.tabs.addTab(scroll(overview_wrap), "Overview")
        self.lineage = LineageView()
        self.tabs.addTab(scroll(self.lineage), "Lineage")
        self.workspace_host = QWidget()
        self.workspace_box = QVBoxLayout(self.workspace_host)
        self.workspace_box.setContentsMargins(0, 8, 0, 8)
        self.workspace_facts = FactGrid()
        self.workspace_notes = label("", "muted", wrap=True)
        self.workspace_box.addWidget(self.workspace_facts)
        self.workspace_box.addSpacing(6)
        self.workspace_box.addWidget(self.workspace_notes)
        self.workspace_box.addStretch(1)
        self.tabs.addTab(scroll(self.workspace_host), "Workspace")
        self.tabs.currentChanged.connect(self._tab_changed)
        box.addWidget(self.tabs, 1)
        return body

    def show_session(self, s: Session | None) -> None:
        self.session = s
        if s is None:
            self.stack.setCurrentIndex(0)
            return
        self.stack.setCurrentIndex(1)
        self.title.setText(s.title or "(untitled)")
        kind = "Imported" if s.imported else "Native"
        origin = f" · began in {s.origin_provider.label}" if s.origin_provider else ""
        flags = "".join(f" · {f}" for f, on in (("archived", s.archived), ("internal agent", s.internal),
                                                ("in use", s.active)) if on)
        self.badge.setText(
            f"{pill_html(s.provider.label, provider_color(s.provider.value))}"
            f' <span style="color:{colors().muted};"> · {kind}{origin}{flags} · </span>'
            f"{pill_html(s.state.label, severity_color(s.state.severity))}")
        self.detail.setText(s.state_detail)
        notes = list(s.warnings)
        if s.duplicate_note:
            notes.append(s.duplicate_note)
        if s.active:
            notes.append(f"In use: {s.active_reason}.")
        self.warning_text.setText("\n".join(notes))
        self.warning_box.setVisible(bool(notes))
        target = s.provider.other
        self.send.setText(f"Send to {target.label}")
        self.send.setEnabled(not s.lineage_error)
        self.open_here.setText(f"Resume in {s.provider.label}")
        self._fill_overview(s)
        self.lineage.fill(s, self.bridge.lineage_of(s))
        self.workspace_facts.fill([("Directory", s.cwd or "(none recorded)")])
        self.workspace_notes.setText("Checking…")
        self._workspace_for = None
        if self.tabs.currentIndex() == 2:
            self._load_workspace()

    def _fill_overview(self, s: Session) -> None:
        rows = [("Session ID", s.session_id), ("Project", s.project or "(none)")]
        if s.cwd and s.cwd != s.project:
            rows.append(("Directory", s.cwd))
        rows += [("Last activity", fmt_time(s.updated_ms)), ("Started", fmt_time(s.created_ms))]
        if s.git_branch:
            rows.append(("Branch at last turn", s.git_branch))
        if s.human_turns is not None:
            line = f"{s.human_turns} human turns"
            if s.imported and s.native_after_import is not None:
                line += f"; {s.native_after_import} added natively since import"
            rows.append(("History", line))
        if s.revisions > 1:
            rows.append(("Rollout revisions", f"{s.revisions} (shown as one conversation)"))
        if s.forked_from:
            rows.append(("Forked from", s.forked_from))
        if s.model:
            rows.append(("Model", s.model))
        if s.source:
            rows.append(("Client", s.source))
        if s.tokens_used:
            rows.append(("Tokens used (provider)", f"{s.tokens_used:,}"))
        if s.size_bytes:
            rows.append(("File size", fmt_size(s.size_bytes)))
        if s.conversation_id:
            rows.append(("Logical conversation", s.conversation_id[:13]))
        if s.path:
            rows.append(("File", _short_path(str(s.path))))
        self.overview.fill(rows)
        if s.path:
            self.overview.grid.itemAtPosition(len(rows) - 1, 1).widget().setToolTip(str(s.path))

    def _tab_changed(self, index: int) -> None:
        if index == 2 and self.session is not None:
            self._load_workspace()

    def _load_workspace(self) -> None:
        s = self.session
        if s is None or self._workspace_for == s.key:
            return
        self._workspace_for = s.key
        run_task(lambda: (s.key, self.bridge.inspect_workspace(s)), on_done=self._workspace_ready,
                 on_failed=lambda msg, _tb: self.workspace_notes.setText(f"Could not inspect: {msg}"),
                 owner=self)

    def _workspace_ready(self, result) -> None:
        key, ws = result
        if self.session is None or self.session.key != key:
            return
        rows = [("Directory", ws.cwd or "(none recorded)")]
        if ws.is_repo:
            rows += [("Repository", ws.root), ("Branch", ws.branch or "unknown"),
                     ("HEAD", f"{ws.head} {ws.head_subject}".strip()),
                     ("Working tree", f"{ws.modified} modified, {ws.staged} staged, {ws.untracked} untracked")]
            if ws.upstream:
                rows.append(("Upstream", f"{ws.upstream}: {ws.ahead} ahead, {ws.behind} behind"))
        self.workspace_facts.fill(rows)
        self.workspace_notes.setText("\n".join(ws.notes) or "Nothing unusual found. This is live "
                                                            "repository state, not part of the transcript.")

    def _send(self) -> None:
        if self.session is not None:
            self.send_requested.emit(self.session, self.session.provider.other)

    def _open(self) -> None:
        s = self.session
        if s is None:
            return
        try:
            open_in_terminal(s.provider, s.session_id, s.cwd)
        except (OSError, ValueError) as err:
            self.detail.setText(f"Could not open a terminal: {err}. Use Copy resume command.")

    def _copy(self) -> None:
        s = self.session
        if s is not None:
            QGuiApplication.clipboard().setText(resume_text(s.provider, s.session_id, s.cwd))
