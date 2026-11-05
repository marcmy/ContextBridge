"""The unified session browser: grouped tree, filtering, sorting and status pills."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import PureWindowsPath

from PySide6.QtCore import QModelIndex, QRect, QSize, QSortFilterProxyModel, Qt
from PySide6.QtGui import QBrush, QColor, QFontMetrics, QPainter, QPen, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QStyle, QStyledItemDelegate, QTreeView

from ..models import Session, SyncState
from .theme import colors, provider_color, severity_color

SESSION_ROLE = Qt.UserRole + 1
SORT_ROLE = Qt.UserRole + 2
GROUP_ROLE = Qt.UserRole + 3

COLUMNS = ["Conversation", "Agent", "Status", "Last activity", "Turns"]
COL_TITLE, COL_AGENT, COL_STATUS, COL_ACTIVITY, COL_TURNS = range(5)

STATUS_FILTERS = [
    ("All statuses", None),
    ("Ready to send", {SyncState.SOURCE_ADVANCED, SyncState.NOT_TRANSFERRED}),
    ("New work elsewhere", {SyncState.DEST_ADVANCED}),
    ("Both advanced", {SyncState.BOTH_ADVANCED}),
    ("Imported, unchanged", {SyncState.UNCHANGED}),
    ("Possible duplicates", {SyncState.POSSIBLE_DUPLICATE}),
    ("Problems", {SyncState.MISSING_DEST, SyncState.MISSING_SOURCE, SyncState.MODIFIED_SOURCE,
                  SyncState.AMBIGUOUS}),
]


def relative_time(ms: int) -> str:
    if not ms:
        return ""
    delta = datetime.now().timestamp() - ms / 1000
    if delta < 90:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} hr ago"
    if delta < 86400 * 14:
        return f"{int(delta // 86400)} d ago"
    return datetime.fromtimestamp(ms / 1000).strftime("%b %d, %Y")


def project_name(project: str) -> str:
    if not project:
        return "No project"
    return PureWindowsPath(project).name or project


class SessionModel(QStandardItemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHorizontalHeaderLabels(COLUMNS)

    def populate(self, sessions: list[Session]) -> None:
        self.removeRows(0, self.rowCount())
        groups: dict[str, list[Session]] = defaultdict(list)
        for s in sessions:
            groups[s.project or s.cwd].append(s)
        ordered = sorted(groups.items(), key=lambda kv: -max(x.updated_ms for x in kv[1]))
        for project, members in ordered:
            head = QStandardItem(project_name(project))
            head.setData(project, GROUP_ROLE)
            head.setData(project, Qt.ToolTipRole)
            head.setData(max(m.updated_ms for m in members), SORT_ROLE)
            head.setEditable(False)
            row = [head] + [self._blank(max(m.updated_ms for m in members)) for _ in COLUMNS[1:]]
            for session in members:
                head.appendRow(self._row(session))
            self.appendRow(row)

    def _blank(self, sort=0) -> QStandardItem:
        item = QStandardItem("")
        item.setEditable(False)
        item.setData(sort, SORT_ROLE)
        return item

    def _row(self, s: Session) -> list[QStandardItem]:
        title = QStandardItem(s.title or s.short_id)
        title.setData(s, SESSION_ROLE)
        title.setData(s.title.casefold(), SORT_ROLE)
        tip = f"{s.title}\n{s.provider.label} {s.session_id}\n{s.cwd}"
        title.setData(tip, Qt.ToolTipRole)
        agent = QStandardItem(s.provider.label)
        agent.setData(s, SESSION_ROLE)
        agent.setData(s.provider.value, SORT_ROLE)
        status = QStandardItem(s.state.label)
        status.setData(s, SESSION_ROLE)
        status.setData(s.state.value, SORT_ROLE)
        status.setData(s.state_detail, Qt.ToolTipRole)
        activity = QStandardItem(relative_time(s.updated_ms))
        activity.setData(s.updated_ms, SORT_ROLE)
        activity.setData(datetime.fromtimestamp(s.updated_ms / 1000).strftime("%Y-%m-%d %H:%M")
                         if s.updated_ms else "", Qt.ToolTipRole)
        activity.setForeground(QBrush(QColor(colors().muted)))
        turns = QStandardItem("" if s.human_turns is None else str(s.human_turns))
        turns.setData(s.human_turns or 0, SORT_ROLE)
        turns.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        turns.setForeground(QBrush(QColor(colors().muted)))
        items = [title, agent, status, activity, turns]
        for item in items:
            item.setEditable(False)
        return items


class SessionFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.search = ""
        self.provider: str | None = None
        self.states: set | None = None
        self.show_archived = False
        self.show_internal = False
        self.setRecursiveFilteringEnabled(True)
        self.setSortRole(SORT_ROLE)
        self.setDynamicSortFilter(True)

    def set(self, **values) -> None:
        for key, value in values.items():
            setattr(self, key, value)
        self.beginFilterChange()
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, row: int, parent: QModelIndex) -> bool:
        model = self.sourceModel()
        index = model.index(row, 0, parent)
        session: Session | None = model.data(index, SESSION_ROLE)
        if session is None:
            return False        # group headers appear when a child matches (recursive filtering)
        if session.archived and not self.show_archived:
            return False
        if session.internal and not self.show_internal:
            return False
        if self.provider and session.provider.value != self.provider:
            return False
        if self.states is not None and session.state not in self.states:
            return False
        if self.search:
            haystack = " ".join((session.title, session.session_id, session.cwd, session.project,
                                 session.git_branch, session.provider.label,
                                 session.state.label)).casefold()
            return all(term in haystack for term in self.search.casefold().split())
        return True


class PillDelegate(QStyledItemDelegate):
    """Draws the agent and status columns as small coloured labels."""

    def paint(self, painter: QPainter, option, index) -> None:
        session: Session | None = index.data(SESSION_ROLE)
        if session is None or index.column() not in (COL_AGENT, COL_STATUS):
            super().paint(painter, option, index)
            return
        self.initStyleOption(option, index)
        text = option.text
        option.text = ""
        style = option.widget.style() if option.widget else None
        if style:
            style.drawControl(QStyle.CE_ItemViewItem, option, painter, option.widget)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(provider_color(session.provider.value) if index.column() == COL_AGENT
                       else severity_color(session.state.severity))
        metrics = QFontMetrics(option.font)
        rect = QRect(option.rect.x() + 6, option.rect.center().y() - 10, 0, 20)
        rect.setWidth(metrics.horizontalAdvance(text) + 16)
        if rect.right() > option.rect.right() - 2:
            rect.setRight(option.rect.right() - 2)
        fill = QColor(color)
        fill.setAlpha(36)
        painter.setPen(QPen(Qt.NoPen))
        painter.setBrush(fill)
        painter.drawRoundedRect(rect, 10, 10)
        painter.setPen(color)
        painter.drawText(rect, Qt.AlignCenter, metrics.elidedText(text, Qt.ElideRight, rect.width() - 10))
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), 30))


class SessionTree(QTreeView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.model_ = SessionModel(self)
        self.proxy = SessionFilter(self)
        self.proxy.setSourceModel(self.model_)
        self.setModel(self.proxy)
        self.setItemDelegate(PillDelegate(self))
        self.setUniformRowHeights(True)
        self.setRootIsDecorated(True)
        self.setAlternatingRowColors(False)
        self.setSortingEnabled(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setIndentation(16)
        header = self.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(COL_TITLE, QHeaderView.Stretch)
        for col, width in ((COL_AGENT, 70), (COL_STATUS, 160), (COL_ACTIVITY, 88), (COL_TURNS, 50)):
            header.setSectionResizeMode(col, QHeaderView.Fixed)
            header.resizeSection(col, width)
        self.sortByColumn(COL_ACTIVITY, Qt.DescendingOrder)

    def populate(self, sessions: list[Session]) -> None:
        current = self.current_session()
        expanded = self._expanded_groups()
        first_load = self.model_.rowCount() == 0
        self.setUpdatesEnabled(False)
        self.model_.populate(sessions)
        self._span_groups()
        self._restore_groups(expanded, first_load)
        if current is not None:
            self.select(current.key)
        self.setUpdatesEnabled(True)

    def _span_groups(self) -> None:
        for row in range(self.proxy.rowCount()):
            self.setFirstColumnSpanned(row, QModelIndex(), True)

    def _expanded_groups(self) -> set[str]:
        out = set()
        for row in range(self.proxy.rowCount()):
            index = self.proxy.index(row, 0)
            if self.isExpanded(index):
                out.add(index.data(GROUP_ROLE))
        return out

    def _restore_groups(self, expanded: set[str], first_load: bool) -> None:
        for row in range(self.proxy.rowCount()):
            index = self.proxy.index(row, 0)
            if first_load or index.data(GROUP_ROLE) in expanded:
                self.expand(index)

    def apply_filter(self, **values) -> None:
        self.proxy.set(**values)
        self._span_groups()
        self.expandAll()

    def current_session(self) -> Session | None:
        index = self.currentIndex()
        return index.siblingAtColumn(0).data(SESSION_ROLE) if index.isValid() else None

    def select(self, key: tuple[str, str]) -> bool:
        for row in range(self.proxy.rowCount()):
            parent = self.proxy.index(row, 0)
            for child in range(self.proxy.rowCount(parent)):
                index = self.proxy.index(child, 0, parent)
                session = index.data(SESSION_ROLE)
                if session is not None and session.key == key:
                    self.expand(parent)
                    self.setCurrentIndex(index)
                    self.scrollTo(index)
                    return True
        return False

    def visible_count(self) -> int:
        return sum(self.proxy.rowCount(self.proxy.index(r, 0)) for r in range(self.proxy.rowCount()))
