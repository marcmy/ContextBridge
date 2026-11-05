"""Colours and the application style sheet, in light and dark."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication


@dataclass(frozen=True)
class Colors:
    window: str
    panel: str
    raised: str
    hover: str
    selected: str
    border: str
    text: str
    muted: str
    faint: str
    accent: str
    accent_text: str
    codex: str
    claude: str
    ok: str
    action: str
    info: str
    warn: str
    danger: str


DARK = Colors(
    window="#111317", panel="#181b20", raised="#20242b", hover="#262b33", selected="#2b3440",
    border="#2d323b", text="#e7e9ec", muted="#a0a7b2", faint="#6d7581",
    accent="#6c8cff", accent_text="#ffffff", codex="#2bb596", claude="#e08a66",
    ok="#4cc36b", action="#6c9dff", info="#98a1ad", warn="#e0ac3f", danger="#f26a5e",
)
LIGHT = Colors(
    window="#f4f5f7", panel="#ffffff", raised="#f0f1f4", hover="#e9ebef", selected="#dfe6f7",
    border="#d9dce2", text="#1b1e24", muted="#5b6470", faint="#8b93a0",
    accent="#3a5ee0", accent_text="#ffffff", codex="#0a8b6e", claude="#c4592f",
    ok="#1f8f43", action="#2c62d6", info="#5e6773", warn="#a97706", danger="#cf3a2d",
)

_current = DARK


def colors() -> Colors:
    return _current


def provider_color(provider: str) -> str:
    return colors().codex if provider == "codex" else colors().claude


def severity_color(severity: str) -> str:
    c = colors()
    return {"ok": c.ok, "action": c.action, "info": c.info, "warn": c.warn, "danger": c.danger}.get(severity, c.info)


def _sheet(c: Colors) -> str:
    return f"""
    * {{ font-family: "Segoe UI Variable Text", "Segoe UI", "Inter", sans-serif; font-size: 13px; }}
    QMainWindow, QDialog {{ background: {c.window}; }}
    QWidget {{ color: {c.text}; }}
    QLabel {{ background: transparent; }}
    QLabel[role="h1"] {{ font-size: 20px; font-weight: 600; }}
    QLabel[role="h2"] {{ font-size: 15px; font-weight: 600; }}
    QLabel[role="muted"] {{ color: {c.muted}; }}
    QLabel[role="faint"] {{ color: {c.faint}; font-size: 12px; }}
    QLabel[role="section"] {{ color: {c.muted}; font-size: 11px; font-weight: 600; }}
    QFrame[role="panel"] {{ background: {c.panel}; border: 1px solid {c.border}; border-radius: 8px; }}
    QFrame[role="card"] {{ background: {c.raised}; border: 1px solid {c.border}; border-radius: 8px; }}
    QFrame[role="banner-warn"] {{ background: transparent; border: 1px solid {c.warn}; border-radius: 6px; }}
    QFrame[role="banner-danger"] {{ background: transparent; border: 1px solid {c.danger}; border-radius: 6px; }}
    QFrame[role="banner-info"] {{ background: transparent; border: 1px solid {c.border}; border-radius: 6px; }}
    QFrame#navRail {{ background: {c.panel}; border-right: 1px solid {c.border}; }}
    QPushButton {{ background: {c.raised}; border: 1px solid {c.border}; border-radius: 6px;
                   padding: 6px 12px; }}
    QPushButton:hover {{ background: {c.hover}; }}
    QPushButton:disabled {{ color: {c.faint}; background: transparent; }}
    QPushButton[role="primary"] {{ background: {c.accent}; border-color: {c.accent};
                                   color: {c.accent_text}; font-weight: 600; }}
    QPushButton[role="primary"]:hover {{ background: {c.accent}; border-color: {c.text}; }}
    QPushButton[role="primary"]:disabled {{ background: {c.raised}; border-color: {c.border};
                                            color: {c.faint}; }}
    QPushButton[role="nav"] {{ background: transparent; border: none; border-radius: 6px;
                               padding: 8px 10px; text-align: left; color: {c.muted}; }}
    QPushButton[role="nav"]:hover {{ background: {c.hover}; }}
    QPushButton[role="nav"]:checked {{ background: {c.selected}; color: {c.text}; font-weight: 600; }}
    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QTextBrowser {{
        background: {c.panel}; border: 1px solid {c.border}; border-radius: 6px; padding: 5px 7px;
        selection-background-color: {c.accent}; selection-color: {c.accent_text}; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {c.accent}; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{ background: {c.panel}; border: 1px solid {c.border};
        selection-background-color: {c.selected}; selection-color: {c.text}; outline: 0; }}
    QTreeView, QTableWidget, QListWidget {{ background: {c.panel}; border: 1px solid {c.border};
        border-radius: 8px; alternate-background-color: {c.panel}; outline: 0; gridline-color: {c.border}; }}
    QTreeView::item, QTableWidget::item {{ padding: 2px 4px; border: none; }}
    QTreeView::item:selected, QTableWidget::item:selected {{ background: {c.selected}; color: {c.text}; }}
    QTreeView::item:hover {{ background: {c.hover}; }}
    QHeaderView {{ background: transparent; }}
    QHeaderView::section {{ background: {c.panel}; color: {c.muted}; border: none;
        border-bottom: 1px solid {c.border}; padding: 6px 8px; font-size: 12px; font-weight: 600; }}
    QTabWidget::pane {{ border: none; }}
    QTabBar::tab {{ background: transparent; color: {c.muted}; padding: 7px 12px;
                    border-bottom: 2px solid transparent; }}
    QTabBar::tab:selected {{ color: {c.text}; border-bottom-color: {c.accent}; font-weight: 600; }}
    QTabBar::tab:hover {{ color: {c.text}; }}
    QScrollArea {{ background: transparent; border: none; }}
    QScrollArea > QWidget > QWidget {{ background: transparent; }}
    QStackedWidget {{ background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c.border}; border-radius: 4px; min-height: 24px; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {c.border}; border-radius: 4px; min-width: 24px; }}
    QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
        background: none; border: none; height: 0; width: 0; }}
    QProgressBar {{ background: {c.raised}; border: none; border-radius: 3px; height: 6px;
                    text-align: center; }}
    QProgressBar::chunk {{ background: {c.accent}; border-radius: 3px; }}
    QStatusBar {{ background: {c.panel}; color: {c.muted}; border-top: 1px solid {c.border}; }}
    QStatusBar::item {{ border: none; }}
    QToolTip {{ background: {c.raised}; color: {c.text}; border: 1px solid {c.border}; padding: 4px; }}
    QCheckBox, QRadioButton {{ spacing: 7px; background: transparent; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 14px; height: 14px;
        border: 1px solid {c.faint}; background: {c.panel}; }}
    QCheckBox::indicator {{ border-radius: 3px; }}
    QRadioButton::indicator {{ border-radius: 8px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {c.accent}; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {c.accent}; border-color: {c.accent}; }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
        background: transparent; border-color: {c.border}; }}
    QCheckBox:disabled, QRadioButton:disabled {{ color: {c.faint}; }}
    QSplitter::handle {{ background: transparent; width: 8px; }}
    QMenu {{ background: {c.panel}; border: 1px solid {c.border}; }}
    QMenu::item {{ padding: 6px 18px; }}
    QMenu::item:selected {{ background: {c.selected}; }}
    """


def apply_theme(app: QApplication) -> None:
    """Follow the system's light or dark setting."""
    global _current
    scheme = QGuiApplication.styleHints().colorScheme()
    _current = LIGHT if scheme == Qt.ColorScheme.Light else DARK
    app.setStyle("Fusion")
    c = _current
    palette = QPalette()
    for role, value in ((QPalette.Window, c.window), (QPalette.WindowText, c.text),
                        (QPalette.Base, c.panel), (QPalette.AlternateBase, c.raised),
                        (QPalette.Text, c.text), (QPalette.Button, c.raised),
                        (QPalette.ButtonText, c.text), (QPalette.Highlight, c.accent),
                        (QPalette.HighlightedText, c.accent_text), (QPalette.ToolTipBase, c.raised),
                        (QPalette.ToolTipText, c.text), (QPalette.PlaceholderText, c.faint),
                        (QPalette.Link, c.accent)):
        palette.setColor(role, QColor(value))
    app.setPalette(palette)
    app.setStyleSheet(_sheet(c))
