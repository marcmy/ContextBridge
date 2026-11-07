"""Inspectable handoff reports: JSON for tools, Markdown for people."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from . import converter


@dataclass
class HandoffReport:
    transfer_id: str
    status: str
    strategy: str
    started_at: str
    source_provider: str
    source_id: str
    source_path: str
    source_title: str
    dest_provider: str
    state_before: str
    state_detail: str
    finished_at: str = ""
    dest_id: str = ""
    dest_path: str = ""
    title: str = ""
    relation: str = ""
    conversation_id: str = ""
    previous_checkpoint: dict = field(default_factory=dict)
    new_checkpoint: dict = field(default_factory=dict)
    new_human: int = 0
    new_assistant: int = 0
    stats: dict = field(default_factory=dict)
    budget: dict | None = None
    summary: dict | None = None
    summary_note: str = ""
    backups: list[str] = field(default_factory=list)
    delta_path: str = ""
    validation: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    workspace: dict = field(default_factory=dict)
    workspace_notes: list[str] = field(default_factory=list)
    follow_up: list[str] = field(default_factory=list)
    error: str = ""
    report_path: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    def to_markdown(self) -> str:
        heading = {"committed": "completed", "assisted_pending": "one manual step left"}.get(
            self.status, self.status)
        lines = [f"# ContextBridge handoff: {heading}", ""]
        if self.error:
            lines += ["## Error", "", self.error, ""]
        lines += [
            "## Sessions", "",
            f"- Source: {self.source_provider} `{self.source_id}`",
            f"  - {self.source_path}",
            f"- Destination: {self.dest_provider} `{self.dest_id or '(not created)'}`",
            f"  - {self.dest_path or '(none)'}",
            f"- Title: {self.title or self.source_title}",
            f"- Transfer type: {self.strategy}",
            f"- Started: {self.started_at}; finished: {self.finished_at}",
            f"- State before: {self.state_before}. {self.state_detail}",
            "",
            "## Checkpoints", "",
            f"- Previous: {_checkpoint_text(self.previous_checkpoint)}",
            f"- New: {_checkpoint_text(self.new_checkpoint)}",
            "- Paths and hashes are in this report's JSON file.",
            "",
            "## Transferred", "",
            f"- New human turns: {self.new_human}",
            f"- New assistant messages: {self.new_assistant}",
        ]
        if self.stats:
            shown = ", ".join(f"{k}: {v}" for k, v in sorted(self.stats.items()))
            lines.append(f"- Converter counts: {shown}")
        if self.budget:
            b = self.budget
            lines += [
                f"- Estimated context: {b.get('before', 0):,} to {b.get('after', 0):,} tokens "
                "(a deliberately conservative estimate; the provider's own count will differ)",
                f"- Records retained: {b.get('retained', 0)}; omitted: {b.get('omitted', 0)}; "
                f"large blocks shortened: {b.get('trimmed', 0)}",
            ]
        if self.summary:
            s = self.summary
            lines.append(f"- Earlier history: method `{s.get('mode')}`, covering "
                         f"{s.get('turns', 0)} earlier human turn(s)")
            if s.get("calls") is not None:
                lines.append(f"- AI summary calls: {s.get('calls')}; cache hits: {s.get('cache_hits')}")
        if self.summary_note:
            lines.append(f"- {self.summary_note}")
        for title, items in (("Validation", self.validation), ("Backups", self.backups),
                             ("Warnings", self.warnings), ("Workspace", self.workspace_notes),
                             ("Follow-up", self.follow_up)):
            if items:
                lines += ["", f"## {title}", ""] + [f"- {item}" for item in items]
        if self.delta_path:
            lines += ["", "## Handoff file", "", f"- {self.delta_path}"]
        return "\n".join(lines) + "\n"


def _checkpoint_text(checkpoint: dict) -> str:
    if not checkpoint or not checkpoint.get("source"):
        return "none"
    source = checkpoint["source"].get("offset")
    destination = (checkpoint.get("destination") or {}).get("offset")
    return f"source byte {source if source is not None else 'unknown'}, " \
           f"destination byte {destination if destination is not None else 'unknown'}"


def write_report(directory: Path, report: HandoffReport) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = directory / f"{stamp}-{report.transfer_id[:8]}"
    report.report_path = str(base.with_suffix(".md"))
    converter.atomic_write_bytes(base.with_suffix(".json"),
                          json.dumps(report.to_dict(), indent=2, ensure_ascii=False).encode("utf-8"))
    converter.atomic_write_bytes(base.with_suffix(".md"), report.to_markdown().encode("utf-8"))
    return base.with_suffix(".md")
