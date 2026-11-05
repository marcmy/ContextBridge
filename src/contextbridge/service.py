"""The one backend the GUI and the `contextbridge` CLI share."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import codex_import as ci

from . import scan
from .codex_bridge import CodexBridge, CodexUnavailable
from .discovery import Discovery
from .handoff import HandoffEngine, HandoffError, HandoffOptions, HandoffPlan
from .lineage import Lineage
from .models import DuplicateCandidate, Link, Provider, Session, SyncState
from .paths import Roots
from .reports import HandoffReport
from .store import Store
from .workspace import Workspace, inspect_workspace

log = logging.getLogger(__name__)
_SAFE_ID = re.compile(r"^[0-9A-Za-z-]{8,64}$")


@dataclass
class Snapshot:
    sessions: list[Session] = field(default_factory=list)
    by_key: dict = field(default_factory=dict)
    links: list[Link] = field(default_factory=list)
    duplicates: list[DuplicateCandidate] = field(default_factory=list)
    transfers: list[dict] = field(default_factory=list)
    archived: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


class CleanupRefused(Exception):
    pass


class ContextBridge:
    def __init__(self, roots: Roots | None = None,
                 bridge_factory: Callable[[], CodexBridge] | None = None, **engine_kwargs):
        self.roots = roots or Roots.default()
        self.roots.activate()
        self.store = Store(self.roots.db_path)
        self.discovery = Discovery(self.roots, self.store)
        self.lineage = Lineage(self.roots, self.store, self.discovery)
        self.engine = HandoffEngine(self.roots, self.store, self.discovery, self.lineage,
                                    bridge_factory=bridge_factory, **engine_kwargs)
        self.bridge_factory = self.engine.bridge_factory
        self.snapshot: Snapshot = Snapshot()
        self._lock = threading.Lock()

    # -- reading ----------------------------------------------------------------------
    def refresh(self, progress: Callable[[str], None] | None = None,
                include_internal: bool = False) -> Snapshot:
        say = progress or (lambda _m: None)
        with self._lock:
            say("Reading Codex threads")
            # Internal threads stay known so a link pointing at one isn't "missing".
            codex_all = self.discovery.codex_sessions(include_internal=True)
            codex = [s for s in codex_all if not s.internal]
            say("Reading Claude transcripts")
            claude = self.discovery.claude_sessions(progress=say)
            self.discovery.mark_recent(codex + claude)
            everything = codex_all + claude
            by_key = {s.key: s for s in everything}
            say("Scanning Codex history")
            for i, s in enumerate(codex):
                if i % 8 == 0:
                    say(f"Scanning Codex history ({i}/{len(codex)})")
                self._fill_codex_counts(s)
            say("Reconciling lineage")
            self.lineage.reconcile(everything)
            self._resolve_assisted()
            self._mark_provenance(by_key)
            for s in claude:
                self._mark_internal_copy(s, by_key)
            sessions = codex + claude
            say("Comparing checkpoints")
            for i, s in enumerate(sessions):
                if i % 8 == 0:
                    say(f"Comparing checkpoints ({i}/{len(sessions)})")
                self._assess(s, by_key)
            say("Looking for duplicates")
            duplicates = self.lineage.duplicates(by_key)
            for candidate in duplicates:
                s = by_key.get(candidate.session)
                if s is None:
                    continue
                s.duplicate_note = candidate.reason
                if s.state in (SyncState.NOT_TRANSFERRED, SyncState.UNCHANGED) and candidate.kind != "content_overlap":
                    s.state = SyncState.POSSIBLE_DUPLICATE
                    s.state_detail = candidate.reason
            for s in sessions:
                if s.lineage_error:
                    s.warnings.append(f"History can't be reconstructed: {s.lineage_error}")
            problems = self._interrupted_notes()
            shown = (sessions + [s for s in codex_all if s.internal] if include_internal
                     else [s for s in sessions if not s.internal])
            snapshot = Snapshot(sessions=sorted(shown, key=lambda s: s.updated_ms, reverse=True),
                                by_key=by_key, links=self.store.links(), duplicates=duplicates,
                                transfers=self.store.transfers(), archived=self.store.archives(),
                                problems=problems)
            self.snapshot = snapshot
        return snapshot

    def _fill_codex_counts(self, s: Session) -> None:
        try:
            info = self.discovery.rollout_scan(s)
        except (OSError, RuntimeError, ValueError, EOFError) as err:
            s.warnings.append(f"Rollout could not be read: {err}")
            return
        s.human_turns = info["human"]
        s.native_after_import = (info["human_after_import"] if info["import_end"] is not None
                                 else info["human"])
        s.fingerprint = info.get("fingerprint", "")
        s.imported = info["import_end"] is not None
        if s.recent and info.get("open_turn"):
            s.active = True
            s.active_reason = "a Codex turn is still running"
        if s.raw.get("lineage_error"):
            s.lineage_error = s.raw["lineage_error"]

    def _mark_provenance(self, by_key: dict) -> None:
        for link in self.store.links():
            if link.relation in ("import", "native_import", "delta_import", "continuation"):
                dst = by_key.get((link.dst_provider.value, link.dst_id))
                if dst is not None:
                    dst.imported = True

    def _mark_internal_copy(self, s: Session, by_key: dict) -> None:
        """A Claude transcript of a Codex reviewer isn't a conversation to browse. It stays
        reachable in the cleanup list and behind the "internal agents" filter."""
        for link in self.store.links_for(Provider.CLAUDE, s.session_id):
            peer = by_key.get((link.src_provider.value, link.src_id))
            if link.dst_id == s.session_id and peer is not None and peer.internal:
                s.internal = not s.native_after_import
                return

    def _assess(self, s: Session, by_key: dict) -> None:
        try:
            a = self.lineage.assess(s, by_key)
        except (OSError, RuntimeError, ValueError, EOFError) as err:
            s.state = SyncState.AMBIGUOUS
            s.state_detail = f"Could not compare checkpoints: {err}"
            return
        s.state = a.state
        s.state_detail = a.detail

    def _resolve_assisted(self) -> None:
        """A file handed over for a manual import: link it once Codex has recorded it."""
        pending = [t for t in self.store.transfers(2000) if t["status"] == "assisted_pending"]
        if not pending:
            return
        import json
        try:
            records = json.loads(self.roots.codex_ledger.read_text(encoding="utf-8")).get("records", [])
        except (OSError, ValueError):
            return
        by_stem = {Path(ci.normalize_path(r.get("source_path"))).stem: r for r in records}
        for transfer in pending:
            intent = json.loads(transfer["intent"] or "{}")
            file = Path(intent.get("assisted_file") or "")
            record = by_stem.get(file.stem)
            if not record:
                continue
            thread = record.get("imported_thread_id")
            sid = transfer["src_id"]
            if file.stem != sid:   # a delta file: it stands for the source session
                link = Link(id=None, src_provider=Provider.CLAUDE, src_id=sid,
                            dst_provider=Provider.CODEX, dst_id=thread, relation="delta_import",
                            origin="contextbridge", src_path=intent.get("src_path", ""),
                            src_offset=intent.get("src_offset"), delta_path=str(file))
                self.store.save_link(link)
            self.store.update_transfer(transfer["id"], status="committed", dst_id=thread)

    def _interrupted_notes(self) -> list[str]:
        notes = []
        for transfer in self.store.interrupted_transfers():
            self.store.update_transfer(transfer["id"], status="interrupted",
                                       error="ContextBridge stopped during this transfer.")
            notes.append(
                f"A transfer of {transfer['src_id'][:13]} was interrupted. Its checkpoint did not "
                f"advance. Check {transfer['dst_path'] or 'the destination'}; backups are in "
                f"{self.roots.claude_home / 'codex-import-backups'}.")
        return notes

    def session(self, provider: Provider | str, session_id: str) -> Session | None:
        return self.snapshot.by_key.get((Provider(provider).value, session_id))

    def find_sessions(self, prefix: str) -> list[Session]:
        return [s for s in self.snapshot.sessions if s.session_id.startswith(prefix)]

    def lineage_of(self, s: Session) -> list[dict]:
        """Every link in this session's logical conversation, oldest first."""
        members = {s.key}
        changed = True
        links = self.store.links()
        while changed:
            changed = False
            for link in links:
                a = (link.src_provider.value, link.src_id)
                b = (link.dst_provider.value, link.dst_id)
                if (a in members) != (b in members):
                    members |= {a, b}
                    changed = True
        out = []
        for link in links:
            if (link.src_provider.value, link.src_id) in members:
                src = self.snapshot.by_key.get((link.src_provider.value, link.src_id))
                dst = self.snapshot.by_key.get((link.dst_provider.value, link.dst_id))
                out.append({"link": link, "src": src, "dst": dst})
        return out

    def inspect_workspace(self, s: Session) -> Workspace:
        return inspect_workspace(s.cwd, s.git_branch)

    # -- handoffs -----------------------------------------------------------------------
    def plan(self, s: Session, target: Provider, options: HandoffOptions,
             strategy: str | None = None) -> HandoffPlan:
        return self.engine.plan(s, target, options, self.snapshot.by_key, strategy)

    def execute(self, plan: HandoffPlan, progress=None, cancel=None) -> HandoffReport:
        report = self.engine.execute(plan, self.snapshot.by_key, progress=progress, cancel=cancel)
        if report.status == "assisted_pending":
            self._note_assisted(report)
        return report

    def _note_assisted(self, report: HandoffReport) -> None:
        import json
        row = self.store.transfer(report.transfer_id)
        intent = json.loads(row["intent"] or "{}")
        cp = report.new_checkpoint.get("source") or {}
        intent.update(assisted_file=report.delta_path, src_path=cp.get("path"),
                      src_offset=cp.get("offset"))
        self.store.update_transfer(report.transfer_id, intent=json.dumps(intent))

    # -- cleanup ------------------------------------------------------------------------
    def can_archive(self, s: Session) -> tuple[bool, str]:
        """Protect active and continued sessions, in code rather than just in the UI."""
        if s.archived:
            return False, "It is already archived."
        if s.provider is Provider.CODEX:
            if s.imported and (s.native_after_import or 0) > 0:
                return False, f"It has {s.native_after_import} turn(s) of native Codex work."
            if not s.imported:
                return False, "It is an original Codex thread, not an imported copy."
        else:
            if not s.imported:
                return False, "It is a native Claude session, not an imported copy."
            if s.native_after_import:
                return False, f"It has {s.native_after_import} turn(s) of native Claude work."
        if s.active:
            return False, f"It looks active ({s.active_reason})."
        if s.recent:
            return False, "It changed in the last few minutes, so it may still be in use."
        return True, ""

    def archive(self, s: Session) -> str:
        """Reversible only. Codex keeps the thread under archived_sessions; a Claude
        transcript moves into ContextBridge's archive folder."""
        allowed, reason = self.can_archive(s)
        if not allowed:
            raise CleanupRefused(reason)
        if s.provider is Provider.CODEX:
            try:
                self.bridge_factory().archive_thread(s.session_id)
            except CodexUnavailable as err:
                raise CleanupRefused(f"Codex could not be reached: {err}. You can also run "
                                     f"`codex archive {s.session_id}` yourself.") from err
            self.store.record_archive("codex", s.session_id, str(s.path), "", "codex-archive")
            return f"Archived in Codex. Restore here, or with `codex unarchive {s.session_id}`."
        source = Path(s.path)
        target = self.roots.archive_dir / "claude" / source.parent.name / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise CleanupRefused(f"An archived copy already exists at {target}.")
        size = source.stat().st_size
        shutil.move(str(source), str(target))
        if target.stat().st_size != size:
            shutil.move(str(target), str(source))
            raise CleanupRefused("The archive copy did not match; the transcript was put back.")
        self.store.record_archive("claude", s.session_id, str(source), str(target), "move")
        return f"Moved to {target}. Nothing was deleted; Restore puts it back."

    def restore(self, provider: str, session_id: str) -> str:
        entry = self.store.active_archive(provider, session_id)
        if entry is None:
            raise CleanupRefused("There is no archive record for this session.")
        if provider == "codex":
            try:
                self.bridge_factory().unarchive_thread(session_id)
            except CodexUnavailable as err:
                raise CleanupRefused(f"Codex could not be reached: {err}") from err
            self.store.mark_restored(provider, session_id)
            return "Restored in Codex."
        original = Path(entry["original_path"])
        if original.exists():
            raise CleanupRefused(f"A file already exists at the original location: {original}")
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(entry["archive_path"], str(original))
        self.store.mark_restored(provider, session_id)
        return f"Restored to {original}."


def resume_command(provider: Provider | str, session_id: str) -> list[str]:
    """The supported command that reopens a session. Session ids come from local files, so
    they are checked before they get near a command line."""
    if not _SAFE_ID.match(session_id or ""):
        raise ValueError(f"Unexpected session id: {session_id!r}")
    if Provider(provider) is Provider.CLAUDE:
        return ["claude", "--resume", session_id]
    return ["codex", "resume", session_id]


def resume_text(provider: Provider | str, session_id: str, cwd: str) -> str:
    command = " ".join(resume_command(provider, session_id))
    return f'cd "{cwd}" && {command}' if cwd else command


def open_in_terminal(provider: Provider | str, session_id: str, cwd: str) -> None:
    """Open a terminal running the resume command, only when the user asks for it."""
    command = resume_command(provider, session_id)
    directory = cwd if cwd and Path(cwd).is_dir() else str(Path.home())
    if sys.platform == "win32":
        wt = shutil.which("wt")
        if wt:
            subprocess.Popen([wt, "-d", directory, "cmd", "/k", *command])
        else:
            subprocess.Popen(["cmd", "/c", "start", "ContextBridge", "/D", directory,
                              "cmd", "/k", *command])
        return
    if sys.platform == "darwin":
        script = f'tell app "Terminal" to do script "cd \\"{directory}\\" && {" ".join(command)}"'
        subprocess.Popen(["osascript", "-e", script])
        return
    for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm"):
        if shutil.which(term):
            subprocess.Popen([term, "-e", *command], cwd=directory)
            return
    raise OSError("No terminal emulator found. Copy the command instead.")


def setup_logging(roots: Roots) -> None:
    roots.app_home.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=roots.log_path, level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
