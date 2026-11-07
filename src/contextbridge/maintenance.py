"""Looking after earlier imports: are they intact, removing them, and Claude Desktop's cards.

Nothing here deletes a transcript or a card. Removed items move into ContextBridge's archive
folder and can be put back, and the ledger is copied before it changes.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import converter, desktop_register, scan
from .paths import Roots
from .store import Store

_TAIL_BYTES = 8192
_HEAD_BYTES = 200_000


@dataclass
class ImportRecord:
    key: str                        # ledger key, or "" when found only by its marker
    title: str
    claude_id: str
    transcript: Path
    thread_id: str = ""
    kind: str = "import"
    mode: str = ""
    cwd: str = ""
    imported_at: str = ""
    source: str = ""
    cards: list[Path] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    native_turns: int = 0
    in_ledger: bool = True

    @property
    def healthy(self) -> bool:
        return not self.problems

    @property
    def is_round_trip(self) -> bool:
        return self.kind == "round-trip"


@dataclass
class RemovalResult:
    moved: list[str] = field(default_factory=list)
    record_only: list[str] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class DeadCard:
    path: Path
    title: str
    account: str


@dataclass
class DesktopAccount:
    path: Path
    label: str
    cards: int
    current: bool


@dataclass
class MoveRow:
    card: Path
    title: str
    cli_id: str
    action: str                     # copy | already_there | no_transcript
    detail: str = ""


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S%f")


def _read_card(path: Path) -> dict | None:
    try:
        card = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return card if isinstance(card, dict) else None


def _account_label(reg: Path) -> str:
    return f"{reg.parent.name[:8]}/{reg.name[:8]}"


class Maintenance:
    def __init__(self, roots: Roots, store: Store):
        self.roots = roots
        self.store = store

    # -- what is on disk -----------------------------------------------------------------
    def _transcripts(self) -> list[Path]:
        return [p for p in sorted(self.roots.claude_projects.glob("*/*.jsonl"))
                if p.parent != self.roots.handoff_project]

    def _cards_by_session(self) -> dict[str, list[Path]]:
        out: dict[str, list[Path]] = {}
        for reg in desktop_register.registries():
            for f in sorted(reg.glob("local_*.json")):
                card = _read_card(f)
                if card and card.get("cliSessionId"):
                    out.setdefault(card["cliSessionId"], []).append(f)
        return out

    @staticmethod
    def _has_import_marker(path: Path) -> str | None:
        """The title of a transcript we wrote, or None. Reads only the two ends of the file."""
        try:
            with path.open("rb") as fh:
                head = fh.read(_HEAD_BYTES).decode("utf-8", "replace")
                fh.seek(max(0, path.stat().st_size - _TAIL_BYTES))
                tail = fh.read().decode("utf-8", "replace")
        except OSError:
            return None
        for line in tail.splitlines():
            if converter.IMPORT_MARK in line and '"custom-title"' in line:
                try:
                    return json.loads(line).get("customTitle") or "(untitled import)"
                except ValueError:
                    return "(untitled import)"
        for line in head.splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "assistant":
                if (rec.get("message") or {}).get("model") == converter.IMPORT_MODEL:
                    return "(untitled import)"
                return None
        return None

    def _source_problem(self, entry: dict) -> str:
        recorded = entry.get("sourceRollout")
        if not recorded:
            return ""
        source = converter.locate_rollout(recorded)
        if not source.exists():
            return "The source Codex rollout is gone."
        expected = entry.get("sourceBytes")
        if expected is None:
            mtime = entry.get("sourceMtime")
            changed = bool(mtime) and int(source.stat().st_mtime) != mtime
        else:
            changed = len(converter.rollout_bytes(source)) != expected
        return "Codex has newer turns than this import." if changed else ""

    def imports(self) -> list[ImportRecord]:
        """Every recorded import and every transcript carrying our marker, with its problems."""
        cards = self._cards_by_session()
        records = []
        known = set()
        for key, entry in converter.load_ledger().items():
            transcript = Path(entry.get("transcript") or "")
            claude_id = entry.get("claudeSessionId") or transcript.stem
            known.add(claude_id)
            record = ImportRecord(
                key=key, title=entry.get("title") or "(untitled import)", claude_id=claude_id,
                transcript=transcript, thread_id=key, kind=entry.get("kind") or "import",
                mode=entry.get("mode") or "", cwd=entry.get("cwd") or "",
                imported_at=entry.get("importedAt") or "", source=entry.get("sourceRollout") or "",
                cards=cards.get(claude_id, []))
            if transcript.is_file():
                record.native_turns = self._native_turns(transcript)
            else:
                record.problems.append("The Claude transcript is missing.")
            problem = self._source_problem(entry)
            if problem:
                record.problems.append(problem)
            records.append(record)
        for path in self._transcripts():
            if path.stem in known:
                continue
            title = self._has_import_marker(path)
            if title is None:
                continue
            records.append(ImportRecord(
                key="", title=title, claude_id=path.stem, transcript=path, in_ledger=False,
                cards=cards.get(path.stem, []), native_turns=self._native_turns(path),
                problems=["Not in the import ledger; found by its import marker."]))
        return sorted(records, key=lambda r: r.imported_at, reverse=True)

    @staticmethod
    def _native_turns(path: Path) -> int:
        try:
            return scan.scan_claude(path)["native_chain"]
        except OSError:
            return 0

    # -- removing imports ----------------------------------------------------------------
    def removal_block(self, record: ImportRecord) -> str:
        """Why this import can't be moved out, or an empty string."""
        if record.is_round_trip:
            return ""       # the session stays; only our record of the sync goes
        if record.native_turns:
            return f"It has {record.native_turns} turn(s) of native Claude work."
        return ""

    def _backup_ledger(self) -> None:
        ledger = self.roots.claude_ledger
        if ledger.is_file():
            self.roots.backups_dir.mkdir(parents=True, exist_ok=True)
            converter.atomic_write_bytes(self.roots.backups_dir / f"codex-imports-{_stamp()}.json",
                                         ledger.read_bytes())

    def remove(self, records: list[ImportRecord], in_use: set[str] | None = None) -> RemovalResult:
        """Move imports and their sidebar rows to the archive; Restore puts all of it back."""
        result = RemovalResult()
        in_use = in_use or set()
        ledger = converter.load_ledger()
        before = dict(ledger)
        for record in records:
            if record.claude_id in in_use:
                result.refused.append((record.title, "It looks like it is in use."))
                continue
            block = self.removal_block(record)
            if block:
                result.refused.append((record.title, block))
                continue
            entry = ledger.get(record.key) if record.key else None
            if record.is_round_trip or not record.transcript.is_file():
                # Either a native session we must keep, or nothing left to move. The dated
                # ledger copy written below is how this entry can be recovered.
                ledger.pop(record.key, None)
                result.record_only.append(record.title)
                continue
            try:
                self._archive_import(record, entry)
            except OSError as err:
                result.refused.append((record.title, str(err)))
                continue
            ledger.pop(record.key, None)
            result.moved.append(record.title)
        if ledger != before:
            self._backup_ledger()
            converter.save_ledger(ledger)
        return result

    def _archive_import(self, record: ImportRecord, entry: dict | None) -> None:
        source = record.transcript
        target = self.roots.archive_dir / "claude" / source.parent.name / source.name
        if target.exists():
            raise OSError(f"An archived copy already exists at {target}.")
        target.parent.mkdir(parents=True, exist_ok=True)
        cards = [{"path": str(c), "text": c.read_text(encoding="utf-8")} for c in record.cards
                 if c.is_file()]
        sidecar = self._sidecar(target)
        converter.atomic_write_bytes(sidecar, json.dumps(
            {"ledgerKey": record.key, "entry": entry, "cards": cards}, indent=2).encode("utf-8"))
        shutil.move(str(source), str(target))
        for card in cards:
            Path(card["path"]).unlink(missing_ok=True)
        self.store.record_archive("claude", record.claude_id, str(source), str(target), "remove-import")

    @staticmethod
    def _sidecar(archived_transcript: Path) -> Path:
        return archived_transcript.with_name(archived_transcript.name + ".removal.json")

    def after_restore(self, archived_transcript: Path) -> None:
        """Put back the ledger entry and sidebar rows that left with a removed import."""
        sidecar = self._sidecar(archived_transcript)
        if not sidecar.is_file():
            return
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        if data.get("ledgerKey") and data.get("entry"):
            ledger = converter.load_ledger()
            if data["ledgerKey"] not in ledger:
                self._backup_ledger()
                ledger[data["ledgerKey"]] = data["entry"]
                converter.save_ledger(ledger)
        for card in data.get("cards", []):
            path = Path(card["path"])
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                converter.atomic_write_bytes(path, card["text"].encode("utf-8"))
        sidecar.unlink()

    # -- dead sidebar rows ---------------------------------------------------------------
    def dead_cards(self) -> list[DeadCard]:
        """Sidebar rows whose transcript is gone: the ones that open on "session not found".

        With no transcripts at all, the Claude folder is probably not the one these cards
        belong to, so nothing is called dead.
        """
        present = {p.stem for p in self.roots.claude_projects.glob("*/*.jsonl")}
        if not present:
            return []
        present |= {a["session_id"] for a in self.store.archives() if a["provider"] == "claude"}
        out = []
        for reg in desktop_register.registries():
            for f in sorted(reg.glob("local_*.json")):
                card = _read_card(f)
                if card and card.get("cliSessionId") and card["cliSessionId"] not in present:
                    out.append(DeadCard(f, card.get("title") or "(untitled)", _account_label(reg)))
        return out

    @property
    def _cards_archive(self) -> Path:
        return self.roots.archive_dir / "cards"

    def _archive_card(self, card: Path) -> None:
        target = self._cards_archive / card.parent.parent.name / card.parent.name / card.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(card), str(target))

    def clear_dead_cards(self, cards: list[DeadCard]) -> int:
        for card in cards:
            self._archive_card(card.path)
        return len(cards)

    def archived_cards(self) -> list[Path]:
        return sorted(self._cards_archive.glob("*/*/local_*.json"))

    def restore_cards(self) -> int:
        """Put every row moved aside back in its account. Rows already there are left alone."""
        restored = 0
        for card in self.archived_cards():
            target = desktop_register.SESSIONS / card.parent.parent.name / card.parent.name / card.name
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(card), str(target))
            restored += 1
        return restored

    # -- moving between accounts ---------------------------------------------------------
    def accounts(self) -> list[DesktopAccount]:
        current = desktop_register.registry_dir()
        return [DesktopAccount(reg, _account_label(reg), len(list(reg.glob("local_*.json"))), reg == current)
                for reg in desktop_register.registries()]

    def plan_move(self, source: Path, destination: Path) -> list[MoveRow]:
        """What copying `source`'s rows to `destination` would do. Writes nothing."""
        if source == destination:
            raise ValueError("The source and destination are the same account.")
        have = desktop_register.cards_by_cli_id(destination)
        present = {p.stem for p in self.roots.claude_projects.glob("*/*.jsonl")}
        rows = []
        for f in sorted(source.glob("local_*.json"), key=lambda p: p.stat().st_mtime):
            card = _read_card(f)
            if card is None:
                continue
            cli = card.get("cliSessionId") or ""
            title = card.get("title") or "(untitled)"
            if cli in have:
                rows.append(MoveRow(f, title, cli, "already_there", "Already in that account."))
            elif not cli or cli not in present:
                rows.append(MoveRow(f, title, cli, "no_transcript", "Its transcript isn't on this PC."))
            else:
                rows.append(MoveRow(f, title, cli, "copy"))
        return rows

    def move_cards(self, source: Path, destination: Path, remove_source: bool = False) -> dict:
        """Copy sidebar rows into another account. With `remove_source` the originals move to
        the archive instead of staying with the old account."""
        template = desktop_register.template_entry(destination)
        if template is None:
            raise ValueError("That account has no sessions to copy settings from. Start one "
                             "session there in Claude Desktop, then try again.")
        rows = self.plan_move(source, destination)
        ledger = converter.load_ledger()
        by_claude = {v.get("claudeSessionId"): key for key, v in ledger.items()}
        ledger_changed = False
        copied = 0
        for row in rows:
            if row.action != "copy":
                continue
            card = _read_card(row.card)
            entry = desktop_register.transfer(card, template)
            target = destination / f"{entry['sessionId']}.json"
            converter.atomic_write_bytes(target, json.dumps(entry, indent=2).encode("utf-8"))
            copied += 1
            if remove_source:
                self._archive_card(row.card)
            if row.cli_id in by_claude:
                ledger[by_claude[row.cli_id]]["card"] = str(target)
                ledger_changed = True
        if ledger_changed:
            self._backup_ledger()
            converter.save_ledger(ledger)
        return {"copied": copied,
                "already_there": sum(r.action == "already_there" for r in rows),
                "no_transcript": sum(r.action == "no_transcript" for r in rows)}
