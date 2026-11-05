"""The handoff flow: review, context settings, confirmation, then the result."""
from __future__ import annotations

import threading
from dataclasses import replace
from html import escape
from pathlib import Path

from PySide6.QtCore import QSettings, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout,
                               QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QRadioButton,
                               QSpinBox, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget)

from ..handoff import HandoffError, HandoffOptions, HandoffPlan
from ..models import Provider, Session
from ..reports import HandoffReport
from ..service import ContextBridge, open_in_terminal, resume_text
from .details import label, scroll
from .theme import colors, provider_color, severity_color
from .workers import run_task

BUDGETS = [("No limit (full history)", None), ("100,000 tokens", 100_000), ("200,000 tokens", 200_000),
           ("400,000 tokens (recommended)", 400_000), ("800,000 tokens", 800_000), ("Custom", -1)]
STEPS = ["Review", "Context", "Confirm", "Result"]


def banner(text: str, role: str = "banner-warn") -> QFrame:
    frame = QFrame()
    frame.setProperty("role", role)
    box = QVBoxLayout(frame)
    box.setContentsMargins(10, 7, 10, 7)
    box.addWidget(label(text, wrap=True))
    return frame


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            clear_layout(item.layout())


def saved_options() -> HandoffOptions:
    s = QSettings("ContextBridge", "ContextBridge")
    defaults = HandoffOptions()
    budget = s.value("budget", defaults.context_budget)
    return HandoffOptions(
        mode=s.value("mode", defaults.mode),
        keep_developer=s.value("keep_developer", defaults.keep_developer, type=bool),
        context_budget=None if budget in (None, "", "none") else int(budget),
        max_tool_chars=s.value("max_tool_chars", defaults.max_tool_chars, type=int),
        summary_mode="index" if s.value("summary_mode", "index") == "claude" else s.value("summary_mode", "index"),
        summary_tokens=s.value("summary_tokens", defaults.summary_tokens, type=int),
        summary_model=s.value("summary_model", defaults.summary_model),
        summary_max_usd=s.value("summary_max_usd", defaults.summary_max_usd, type=float),
        summary_max_calls=s.value("summary_max_calls", defaults.summary_max_calls, type=int),
    )


def save_options(o: HandoffOptions) -> None:
    s = QSettings("ContextBridge", "ContextBridge")
    s.setValue("budget", "none" if o.context_budget is None else o.context_budget)
    for key in ("mode", "keep_developer", "max_tool_chars", "summary_mode", "summary_tokens",
                "summary_model", "summary_max_usd", "summary_max_calls"):
        s.setValue(key, getattr(o, key))


class HandoffDialog(QDialog):
    transfer_finished = Signal(object)

    def __init__(self, bridge: ContextBridge, session: Session, target: Provider, parent=None):
        super().__init__(parent)
        self.bridge = bridge
        self.session = session
        self.target = target
        self.plan: HandoffPlan | None = None
        self.report: HandoffReport | None = None
        self.options = saved_options()
        self.strategy: str | None = None
        self.cancel_event = threading.Event()
        self._generation = 0
        self._busy = False
        self.setWindowTitle(f"Send to {target.label}")
        self.setMinimumSize(720, 620)
        self.resize(780, 680)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 14)
        root.setSpacing(10)
        title = label(f"Send to {target.label}", "h1")
        root.addWidget(title)
        route = QLabel(
            f'<span style="color:{provider_color(session.provider.value)}; font-weight:600;">'
            f"{session.provider.label}</span> {escape(session.title[:70])}  →  "
            f'<span style="color:{provider_color(target.value)}; font-weight:600;">{target.label}</span>')
        route.setTextFormat(Qt.RichText)
        route.setWordWrap(True)
        root.addWidget(route)
        self.steps = QLabel()
        self.steps.setTextFormat(Qt.RichText)
        root.addWidget(self.steps)

        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.review_page = self._build_review()
        self.context_page = self._build_context()
        self.confirm_page = self._build_confirm()
        self.result_page = self._build_result()
        for page in (self.review_page, self.context_page, self.confirm_page, self.result_page):
            self.pages.addWidget(page)

        buttons = QHBoxLayout()
        self.progress_text = label("", "muted")
        self.back_button = QPushButton("Back")
        self.next_button = QPushButton("Next")
        self.next_button.setProperty("role", "primary")
        self.close_button = QPushButton("Cancel")
        self.back_button.clicked.connect(self._back)
        self.next_button.clicked.connect(self._next)
        self.close_button.clicked.connect(self._close_clicked)
        buttons.addWidget(self.progress_text, 1)
        buttons.addWidget(self.close_button)
        buttons.addWidget(self.back_button)
        buttons.addWidget(self.next_button)
        root.addLayout(buttons)

        self._replan_timer = QTimer(self)
        self._replan_timer.setSingleShot(True)
        self._replan_timer.setInterval(350)
        self._replan_timer.timeout.connect(self._replan)
        self._load_options_into_form()
        self._go(0)
        self._replan()

    # -- pages -------------------------------------------------------------------------
    def _build_review(self) -> QWidget:
        body = QWidget()
        self.review_box = QVBoxLayout(body)
        self.review_box.setContentsMargins(0, 4, 8, 4)
        self.review_box.setSpacing(8)
        self.state_label = QLabel()
        self.state_label.setTextFormat(Qt.RichText)
        self.state_label.setWordWrap(True)
        self.review_box.addWidget(self.state_label)
        self.review_box.addWidget(label("WHAT TO DO", "section"))
        self.strategy_host = QVBoxLayout()
        self.strategy_host.setSpacing(6)
        self.review_box.addLayout(self.strategy_host)
        self.review_box.addWidget(label("BEFORE YOU HAND OFF", "section"))
        self.review_notes = QVBoxLayout()
        self.review_notes.setSpacing(6)
        self.review_box.addLayout(self.review_notes)
        self.review_box.addStretch(1)
        self.strategy_group = QButtonGroup(self)
        return scroll(body)

    def _build_context(self) -> QWidget:
        body = QWidget()
        box = QVBoxLayout(body)
        box.setContentsMargins(0, 4, 8, 4)
        box.setSpacing(8)
        self.numbers = QFrame()
        self.numbers.setProperty("role", "card")
        numbers_box = QVBoxLayout(self.numbers)
        numbers_box.setContentsMargins(12, 10, 12, 10)
        self.numbers_text = QLabel()
        self.numbers_text.setTextFormat(Qt.RichText)
        self.numbers_text.setWordWrap(True)
        numbers_box.addWidget(self.numbers_text)
        box.addWidget(self.numbers)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(8)
        self.budget = QComboBox()
        for text, value in BUDGETS:
            self.budget.addItem(text, value)
        self.budget_custom = QSpinBox()
        self.budget_custom.setRange(5000, 5_000_000)
        self.budget_custom.setSingleStep(10_000)
        self.budget_custom.setSuffix(" tokens")
        budget_row = QHBoxLayout()
        budget_row.addWidget(self.budget, 2)
        budget_row.addWidget(self.budget_custom, 1)
        self.budget_label = label("Context budget")
        form.addRow(self.budget_label, budget_row)
        self.max_tool = QSpinBox()
        self.max_tool.setRange(100, 200_000)
        self.max_tool.setSingleStep(500)
        self.max_tool.setSuffix(" characters")
        self.max_tool_label = label("Shorten tool output over")
        form.addRow(self.max_tool_label, self.max_tool)
        self.mode = QComboBox()
        self.mode.addItem("Faithful: real tool calls, renders like a native session", "faithful")
        self.mode.addItem("Archive: tool activity as readable text", "archive")
        self.mode_label = label("Conversion")
        form.addRow(self.mode_label, self.mode)
        self.keep_dev = QCheckBox("Keep Codex developer instructions as background messages")
        self.keep_dev_label = label("")
        form.addRow(self.keep_dev_label, self.keep_dev)
        self.summary_mode = QComboBox()
        self.summary_mode.addItem("Offline evidence index: quoted excerpts, free", "index")
        self.summary_mode.addItem("AI summary by Claude: uses your Claude account", "claude")
        self.summary_mode.addItem("Off: only a list of earlier requests", "off")
        form.addRow(label("Earlier history"), self.summary_mode)
        self.summary_tokens = QSpinBox()
        self.summary_tokens.setRange(1000, 30000)
        self.summary_tokens.setSingleStep(1000)
        self.summary_tokens.setSuffix(" tokens")
        form.addRow(label("History note size"), self.summary_tokens)
        box.addLayout(form)

        self.ai_frame = QFrame()
        self.ai_frame.setProperty("role", "banner-warn")
        ai = QVBoxLayout(self.ai_frame)
        ai.setContentsMargins(12, 10, 12, 10)
        ai.addWidget(label("An AI summary sends excerpts of the omitted history to Claude and uses "
                           "your Claude Code usage. Results are fallible; citations are checked "
                           "against the source turns. The dollar limit applies per call, so it is "
                           "not a guaranteed total.", wrap=True))
        ai_form = QFormLayout()
        self.ai_model = QComboBox()
        self.ai_model.addItems(["sonnet", "opus", "haiku"])
        self.ai_model.setEditable(True)
        ai_form.addRow(label("Model"), self.ai_model)
        self.ai_usd = QDoubleSpinBox()
        self.ai_usd.setRange(0.01, 100)
        self.ai_usd.setPrefix("$")
        self.ai_usd.setSingleStep(0.5)
        ai_form.addRow(label("Limit per call"), self.ai_usd)
        self.ai_calls = QSpinBox()
        self.ai_calls.setRange(1, 200)
        ai_form.addRow(label("Maximum calls"), self.ai_calls)
        ai.addLayout(ai_form)
        self.ai_authorized = QCheckBox("I authorize this summary and its usage for this transfer")
        ai.addWidget(self.ai_authorized)
        box.addWidget(self.ai_frame)
        self.desktop = QCheckBox("Also add a row to Claude Desktop's sidebar (quit and reopen it afterwards)")
        box.addWidget(self.desktop)
        box.addStretch(1)

        self.budget.currentIndexChanged.connect(self._form_changed)
        self.budget_custom.valueChanged.connect(self._form_changed)
        for widget in (self.max_tool, self.summary_tokens, self.ai_calls):
            widget.valueChanged.connect(self._form_changed)
        self.ai_usd.valueChanged.connect(self._form_changed)
        self.mode.currentIndexChanged.connect(self._form_changed)
        self.summary_mode.currentIndexChanged.connect(self._form_changed)
        self.ai_model.currentTextChanged.connect(self._form_changed)
        self.keep_dev.toggled.connect(self._form_changed)
        self.ai_authorized.toggled.connect(self._authorization_changed)
        self.desktop.toggled.connect(self._authorization_changed)
        return scroll(body)

    def _build_confirm(self) -> QWidget:
        self.confirm_text = QTextBrowser()
        self.confirm_text.setOpenExternalLinks(False)
        return self.confirm_text

    def _build_result(self) -> QWidget:
        body = QWidget()
        box = QVBoxLayout(body)
        box.setContentsMargins(0, 4, 0, 4)
        box.setSpacing(8)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        box.addWidget(self.progress)
        self.result_title = label("Working…", "h2", wrap=True)
        box.addWidget(self.result_title)
        self.result_text = QTextBrowser()
        self.result_text.setOpenExternalLinks(False)
        box.addWidget(self.result_text, 1)
        actions = QHBoxLayout()
        self.open_destination = QPushButton("Open in destination")
        self.open_destination.setProperty("role", "primary")
        self.open_destination.clicked.connect(self._open_destination)
        self.copy_resume = QPushButton("Copy resume command")
        self.copy_resume.clicked.connect(self._copy_resume)
        self.open_report = QPushButton("Open report")
        self.open_report.clicked.connect(self._open_report)
        for button in (self.open_destination, self.copy_resume, self.open_report):
            button.setVisible(False)
            actions.addWidget(button)
        actions.addStretch(1)
        box.addLayout(actions)
        return body

    # -- navigation --------------------------------------------------------------------
    def _go(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        parts = []
        c = colors()
        for i, name in enumerate(STEPS):
            color = c.text if i == index else c.faint
            weight = 600 if i == index else 400
            parts.append(f'<span style="color:{color}; font-weight:{weight};">{i + 1}. {name}</span>')
        self.steps.setText('<span style="color:%s;">  ·  </span>'.join(parts) % ((c.faint,) * (len(parts) - 1)))
        self._sync_buttons()
        if index == 2:
            self._fill_confirm()

    def _sync_buttons(self) -> None:
        index = self.pages.currentIndex()
        plan = self.plan
        planning = self._busy
        if index == 3:
            self.back_button.setVisible(False)
            self.next_button.setVisible(False)
            self.close_button.setText("Close")
            return
        self.back_button.setVisible(True)
        self.back_button.setEnabled(index > 0)
        self.next_button.setVisible(True)
        self.close_button.setText("Cancel")
        ready = plan is not None and plan.can_execute and not planning
        if index == 2:
            self.next_button.setText("Start transfer")
            needs_ai = self._ai_applies() and self.options.summary_mode == "claude"
            ready = ready and (not needs_ai or self.options.summary_authorized)
        else:
            self.next_button.setText("Next")
        self.next_button.setEnabled(ready)

    def _back(self) -> None:
        self._go(max(0, self.pages.currentIndex() - 1))

    def _next(self) -> None:
        index = self.pages.currentIndex()
        if index == 2:
            self._start_transfer()
        else:
            self._go(index + 1)

    def _close_clicked(self) -> None:
        if self.pages.currentIndex() == 3 and self.report is None:
            self.cancel_event.set()
            self.result_title.setText("Cancelling after the current step…")
            return
        self.accept()

    def reject(self) -> None:
        if self.pages.currentIndex() == 3 and self.report is None:
            self.cancel_event.set()
            return
        super().reject()

    # -- options -----------------------------------------------------------------------
    def _load_options_into_form(self) -> None:
        o = self.options
        widgets = (self.budget, self.budget_custom, self.max_tool, self.mode, self.keep_dev,
                   self.summary_mode, self.summary_tokens, self.ai_model, self.ai_usd, self.ai_calls)
        for w in widgets:
            w.blockSignals(True)
        value = o.context_budget
        match = next((i for i, (_, b) in enumerate(BUDGETS) if b == value), None)
        if match is None:
            match = len(BUDGETS) - 1
            self.budget_custom.setValue(value or 400_000)
        else:
            self.budget_custom.setValue(value or 400_000)
        self.budget.setCurrentIndex(match)
        self.budget_custom.setVisible(BUDGETS[match][1] == -1)
        self.max_tool.setValue(o.max_tool_chars)
        self.mode.setCurrentIndex(max(0, self.mode.findData(o.mode)))
        self.keep_dev.setChecked(o.keep_developer)
        self.summary_mode.setCurrentIndex(max(0, self.summary_mode.findData(o.summary_mode)))
        self.summary_tokens.setValue(o.summary_tokens)
        self.ai_model.setCurrentText(o.summary_model)
        self.ai_usd.setValue(o.summary_max_usd)
        self.ai_calls.setValue(o.summary_max_calls)
        for w in widgets:
            w.blockSignals(False)
        self.ai_frame.setVisible(o.summary_mode == "claude")

    def _read_form(self) -> HandoffOptions:
        selected = self.budget.currentData()
        budget = self.budget_custom.value() if selected == -1 else selected
        return replace(
            self.options, context_budget=budget, max_tool_chars=self.max_tool.value(),
            mode=self.mode.currentData(), keep_developer=self.keep_dev.isChecked(),
            summary_mode=self.summary_mode.currentData(), summary_tokens=self.summary_tokens.value(),
            summary_model=self.ai_model.currentText().strip() or "sonnet",
            summary_max_usd=self.ai_usd.value(), summary_max_calls=self.ai_calls.value(),
            summary_authorized=self.ai_authorized.isChecked() and self.summary_mode.currentData() == "claude",
            register_desktop=self.desktop.isChecked())

    def _form_changed(self, *_args) -> None:
        self.budget_custom.setVisible(self.budget.currentData() == -1)
        self.ai_frame.setVisible(self.summary_mode.currentData() == "claude")
        if self.summary_mode.currentData() != "claude":
            self.ai_authorized.setChecked(False)
        self.options = self._read_form()
        self.numbers_text.setText(f'<span style="color:{colors().muted};">Recalculating…</span>')
        self._busy = True
        self._sync_buttons()
        self._replan_timer.start()

    def _authorization_changed(self, *_args) -> None:
        self.options = self._read_form()
        if self.plan is not None:
            self.plan.options = self.options
        self._sync_buttons()

    def _ai_applies(self) -> bool:
        return self.plan is not None and self.plan.strategy in ("new_session", "continuation", "replace", "delta_import")

    # -- planning ----------------------------------------------------------------------
    def _replan(self) -> None:
        self._generation += 1
        generation = self._generation
        self._busy = True
        self._sync_buttons()
        self.progress_text.setText("Calculating the preview…")
        options, strategy, session, target = replace(self.options), self.strategy, self.session, self.target
        run_task(lambda: self.bridge.plan(session, target, options, strategy),
                 on_done=lambda plan: self._plan_ready(generation, plan),
                 on_failed=lambda msg, _tb: self._plan_failed(generation, msg), owner=self)

    def _plan_failed(self, generation: int, message: str) -> None:
        if generation != self._generation:
            return
        self._busy = False
        self.progress_text.setText("")
        self.plan = None
        self.state_label.setText(escape(message))
        self._sync_buttons()

    def _plan_ready(self, generation: int, plan: HandoffPlan) -> None:
        if generation != self._generation:
            return
        self._busy = False
        self.progress_text.setText("")
        self.plan = plan
        self.strategy = plan.strategy
        self._fill_review(plan)
        self._fill_numbers(plan)
        self._sync_buttons()
        if self.pages.currentIndex() == 2:
            self._fill_confirm()

    def _fill_review(self, plan: HandoffPlan) -> None:
        state = plan.assessment.state
        self.state_label.setText(
            f'<span style="color:{severity_color(state.severity)}; font-weight:600;">{state.label}.</span> '
            f"{escape(plan.assessment.detail)}")
        clear_layout(self.strategy_host)
        for button in self.strategy_group.buttons():
            self.strategy_group.removeButton(button)
        for option in plan.strategies:
            radio = QRadioButton(option.label + ("  (recommended)" if option.recommended else ""))
            radio.setProperty("strategy", option.key)
            radio.setChecked(option.key == plan.strategy)
            radio.setEnabled(option.enabled)
            radio.toggled.connect(self._strategy_toggled)
            self.strategy_group.addButton(radio)
            self.strategy_host.addWidget(radio)
            details = option.description
            if option.caution:
                details = (details + " " + option.caution).strip()
            if details:
                note = label(details, "faint", wrap=True)
                note.setContentsMargins(24, 0, 0, 2)
                self.strategy_host.addWidget(note)
        clear_layout(self.review_notes)
        if plan.blocked:
            self.review_notes.addWidget(banner(plan.blocked, "banner-danger"))
        for warning in plan.warnings:
            self.review_notes.addWidget(banner(warning))
        ws = plan.workspace
        if ws and ws.is_repo:
            line = (f"{ws.root} on {ws.branch} at {ws.head}: {ws.modified} modified, "
                    f"{ws.staged} staged, {ws.untracked} untracked.")
            self.review_notes.addWidget(banner(line, "banner-info"))
        for note in plan.workspace_notes:
            self.review_notes.addWidget(banner(note, "banner-info"))
        self.review_notes.addWidget(banner(
            "This moves conversation history. Code, branches, dependencies and running processes "
            "stay where they are, and the destination agent should verify them.", "banner-info"))
        # Settings that don't apply to this transfer stay out of the way.
        to_claude = self.target is Provider.CLAUDE
        for widget in (self.budget_label, self.budget, self.max_tool_label, self.max_tool):
            widget.setVisible(to_claude or plan.strategy.startswith("delta"))
        self.budget_custom.setVisible(self.budget.currentData() == -1 and self.budget.isVisibleTo(self))
        for widget in (self.mode_label, self.mode, self.keep_dev_label, self.keep_dev, self.desktop):
            widget.setVisible(to_claude)

    def _strategy_toggled(self, checked: bool) -> None:
        if not checked:
            return
        key = self.sender().property("strategy")
        if self.plan is not None and key != self.plan.strategy:
            self.strategy = key
            self._replan()

    def _fill_numbers(self, plan: HandoffPlan) -> None:
        c = colors()
        lines = []
        if plan.strategy == "none" or plan.blocked:
            lines.append(f'<span style="color:{c.muted};">Nothing would be transferred.</span>')
        else:
            if plan.estimated_after is not None:
                before = plan.estimated_before
                est = (f"<b>{before:,}</b> → <b>{plan.estimated_after:,}</b> estimated tokens"
                       if before and before != plan.estimated_after
                       else f"<b>{plan.estimated_after:,}</b> estimated tokens")
                lines.append(est + f' <span style="color:{c.muted};">(conservative; the agent\'s own '
                                   "count will differ)</span>")
            if plan.budget:
                b = plan.budget
                lines.append(f"Keeps {b.get('retained', 0):,} recent records, omits {b.get('omitted', 0):,}, "
                             f"shortens {b.get('trimmed', 0):,} large blocks.")
            if plan.summary:
                lines.append(f"Earlier history covers {plan.summary.get('turns', 0)} human turn(s).")
            lines.append(f"Transfers {plan.new_human} human turn(s) and {plan.new_assistant} assistant message(s).")
            if plan.summary_note:
                lines.append(f'<span style="color:{c.muted};">{escape(plan.summary_note)}</span>')
        self.numbers_text.setText("<br>".join(lines))

    def _fill_confirm(self) -> None:
        plan = self.plan
        if plan is None:
            return
        c = colors()
        option = plan.option(plan.strategy)
        rows = [f"<h3>{escape(option.label if option else plan.strategy)}</h3>"]
        if option:
            rows.append(f"<p>{escape(option.description)}</p>")
        rows.append("<ul>")
        rows.append(f"<li>From {plan.source.provider.label} <code>{plan.source.session_id}</code></li>")
        if plan.dest_id:
            rows.append(f"<li>To {plan.target.label} <code>{plan.dest_id}</code></li>")
        else:
            rows.append(f"<li>To a new {plan.target.label} session</li>")
        if plan.dest_path:
            rows.append(f"<li>In <code>{escape(plan.dest_path)}</code></li>")
        if plan.estimated_after is not None:
            rows.append(f"<li>About {plan.estimated_after:,} estimated tokens</li>")
        if self.options.context_budget and plan.budget:
            rows.append(f"<li>Budget {self.options.context_budget:,}: {plan.budget.get('omitted', 0):,} "
                        "older records left out, recorded in a note at the top</li>")
        if plan.strategy in ("append", "replace"):
            rows.append("<li>A backup of the existing transcript is made first</li>")
        rows.append("<li>Your source session is not modified; no historical command is run</li>")
        if self.options.summary_mode == "claude" and self._ai_applies():
            state = "authorized" if self.options.summary_authorized else "<b>not authorized yet: go back to Context</b>"
            rows.append(f"<li>AI summary with {escape(self.options.summary_model)}: {state}</li>")
        rows.append("</ul>")
        for warning in plan.warnings:
            rows.append(f'<p style="color:{c.warn};">{escape(warning)}</p>')
        self.confirm_text.setHtml("".join(rows))
        self._sync_buttons()

    # -- running -----------------------------------------------------------------------
    def _start_transfer(self) -> None:
        plan = self.plan
        if plan is None or not plan.can_execute:
            return
        self.options = self._read_form()
        plan.options = self.options
        save_options(self.options)
        self._go(3)
        self.result_title.setText("Starting…")
        self.close_button.setText("Cancel")
        bridge = self.bridge
        cancel = self.cancel_event
        run_task(lambda say: bridge.execute(plan, progress=say, cancel=cancel),
                 on_progress=self.result_title.setText, on_done=self._finished,
                 on_failed=self._failed, owner=self)

    def _failed(self, message: str, _trace: str) -> None:
        self.report = HandoffReport(transfer_id="", status="failed", strategy="", started_at="",
                                    source_provider="", source_id="", source_path="", source_title="",
                                    dest_provider="", state_before="", state_detail="", error=message)
        self.progress.setVisible(False)
        self.result_title.setText("The transfer did not complete")
        self.result_text.setHtml(f'<p style="color:{colors().danger};">{escape(message)}</p>'
                                 "<p>Your source session was not changed. Any existing destination "
                                 "transcript was left as it was or restored from its backup.</p>")
        self.close_button.setText("Close")
        self.transfer_finished.emit(None)

    def _finished(self, report: HandoffReport) -> None:
        self.report = report
        self.progress.setVisible(False)
        self.close_button.setText("Close")
        titles = {"committed": "Transferred and checked", "cancelled": "Cancelled before writing",
                  "assisted_pending": "One manual step left", "failed": "The transfer did not complete"}
        self.result_title.setText(titles.get(report.status, report.status))
        self.result_text.setMarkdown(report.to_markdown())
        ok = report.status == "committed" and bool(report.dest_id)
        self.open_destination.setVisible(ok)
        self.open_destination.setText(f"Open in {self.target.label}")
        self.copy_resume.setVisible(ok)
        self.open_report.setVisible(bool(report.report_path))
        self.transfer_finished.emit(report)

    def _dest_cwd(self) -> str:
        return self.session.cwd

    def _open_destination(self) -> None:
        if self.report and self.report.dest_id:
            try:
                open_in_terminal(self.target, self.report.dest_id, self._dest_cwd())
            except (OSError, ValueError) as err:
                self.result_title.setText(f"Could not open a terminal: {err}")

    def _copy_resume(self) -> None:
        if self.report and self.report.dest_id:
            QGuiApplication.clipboard().setText(resume_text(self.target, self.report.dest_id, self._dest_cwd()))

    def _open_report(self) -> None:
        if self.report and self.report.report_path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.report.report_path))))


__all__ = ["HandoffDialog", "HandoffError"]
