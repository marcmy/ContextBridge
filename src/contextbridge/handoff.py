"""Planning and executing handoffs.

A plan writes nothing and makes no billable call. Execution re-checks that nothing
moved since the preview, records its intent before touching any file, backs up what it
will change, validates what it wrote, and only then advances the checkpoints.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import codex_import as ci
import desktop_register
from context_budget import bound_record, estimate_tokens
from history_summary import _fit_bytes, claude_summary, evidence_turns, offline_digest

from . import __version__, scan
from .codex_bridge import CodexBridge, CodexUnavailable
from .discovery import Discovery, RolloutIndex
from .lineage import Lineage, codex_records_after
from .models import Assessment, Link, Provider, Session, SyncState
from .paths import Roots
from .reports import HandoffReport, write_report
from .store import Store, now_iso
from .workspace import Workspace, compare_workspaces, inspect_workspace

log = logging.getLogger(__name__)
CODEX, CLAUDE = Provider.CODEX, Provider.CLAUDE


class HandoffError(Exception):
    """A handoff refused or failed. The message is written for the person using the app."""


class HandoffCancelled(HandoffError):
    pass


@dataclass
class HandoffOptions:
    """Defaults match the settings proven on a real 1.1M-token thread."""
    mode: str = "faithful"
    keep_developer: bool = True
    context_budget: int | None = 400_000
    max_tool_chars: int = 3000
    summary_mode: str = "index"          # off | index | claude
    summary_tokens: int = 8000
    summary_model: str = "sonnet"
    summary_max_usd: float = 2.0
    summary_max_calls: int = 16
    summary_authorized: bool = False     # AI summaries also need this explicit yes
    title_prefix: str = ""
    group_scratch: bool = True
    register_desktop: bool = False

    def validate(self) -> None:
        if self.context_budget is not None and self.context_budget < 5000:
            raise HandoffError("The context budget must be at least 5,000 tokens.")
        if not 1000 <= self.summary_tokens <= 30000:
            raise HandoffError("The summary budget must be between 1,000 and 30,000 tokens.")
        if self.max_tool_chars < 100:
            raise HandoffError("Tool output limit must be at least 100 characters.")
        if (self.context_budget is not None and self.summary_mode != "off"
                and self.context_budget <= self.summary_tokens + 4000):
            raise HandoffError("The context budget must leave 4,000 tokens beyond the summary.")
        if self.summary_mode == "claude" and self.summary_max_usd <= 0:
            raise HandoffError("The per-call summary limit must be positive.")

    def as_args(self, claude_dir: Path, *, dry_run: bool) -> SimpleNamespace:
        """The attribute set the shared converter takes."""
        return SimpleNamespace(
            mode=self.mode, keep_developer=self.keep_developer, no_banner=False,
            no_group_scratch=not self.group_scratch, context_budget=self.context_budget,
            max_tool_chars=self.max_tool_chars, summary_mode=self.summary_mode,
            summary_file=None, summary_tokens=self.summary_tokens,
            summary_model=self.summary_model, summary_max_usd=self.summary_max_usd,
            summary_max_calls=self.summary_max_calls, title_prefix=self.title_prefix,
            claude_dir=str(claude_dir), dry_run=dry_run, replace=False)


@dataclass
class StrategyOption:
    key: str
    label: str
    description: str
    enabled: bool = True
    recommended: bool = False
    caution: str = ""


@dataclass
class HandoffPlan:
    source: Session
    target: Provider
    assessment: Assessment
    strategies: list[StrategyOption]
    strategy: str
    options: HandoffOptions
    workspace: Workspace | None = None
    workspace_notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blocked: str = ""
    dest_id: str = ""
    dest_path: str = ""
    # Dry-run numbers.
    stats: dict = field(default_factory=dict)
    budget: dict | None = None
    summary: dict | None = None
    new_human: int = 0
    new_assistant: int = 0
    estimated_before: int | None = None
    estimated_after: int | None = None
    summary_note: str = ""
    # State to re-check at execution.
    source_state: tuple[int, int] | None = None
    dest_state: tuple[int, int] | None = None
    created_at: str = field(default_factory=now_iso)

    @property
    def can_execute(self) -> bool:
        return not self.blocked and self.strategy != "none"

    def option(self, key: str) -> StrategyOption | None:
        return next((o for o in self.strategies if o.key == key), None)


@dataclass
class _Outcome:
    dest_id: str = ""
    dest_path: str = ""
    relation: str = ""
    backup: str = ""
    src_pos: tuple[str, int | None, str | None] = ("", None, None)
    dst_pos: tuple[str, int | None, str | None] = ("", None, None)
    delta_path: str = ""
    stats: dict = field(default_factory=dict)
    budget: dict | None = None
    summary: dict | None = None
    new_human: int = 0
    new_assistant: int = 0
    validation: list[str] = field(default_factory=list)
    follow_up: list[str] = field(default_factory=list)
    status: str = "committed"
    title: str = ""


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S%f")


class HandoffEngine:
    def __init__(self, roots: Roots, store: Store, discovery: Discovery, lineage: Lineage,
                 bridge_factory: Callable[[], CodexBridge] | None = None,
                 version_probe: Callable[[], str] = ci.claude_version,
                 desktop_probe: Callable[[], bool] = desktop_register.desktop_running):
        self.roots = roots
        self.store = store
        self.discovery = discovery
        self.lineage = lineage
        self.bridge_factory = bridge_factory or self._default_bridge
        self._version_probe = version_probe
        self._version: str | None = None
        self.desktop_probe = desktop_probe

    def version_probe(self) -> str:
        """The Claude Code version stamped on records; looked up once, as it spawns a process."""
        if self._version is None:
            self._version = self._version_probe()
        return self._version

    def _default_bridge(self) -> CodexBridge:
        home = None
        if self.roots.claude_home != Path.home() / ".claude" and self.roots.claude_home.name == ".claude":
            home = self.roots.claude_home.parent
        return CodexBridge(self.roots.codex_home, user_home=home)

    # ------------------------------------------------------------------ planning
    def plan(self, source: Session, target: Provider, options: HandoffOptions, sessions: dict,
             strategy: str | None = None) -> HandoffPlan:
        """Decide what a transfer would do and measure it. Writes nothing."""
        if source.provider is target:
            raise HandoffError(f"This session is already in {target.label}.")
        options.validate()
        if source.provider is CODEX:
            self.discovery.hydrate_codex(source)
        assessment = self.lineage.assess(source, sessions, target)
        strategies = (self._codex_strategies(source, assessment, sessions)
                      if source.provider is CODEX
                      else self._claude_strategies(source, assessment, sessions))
        chosen = strategy or next((o.key for o in strategies if o.recommended), "none")
        plan = HandoffPlan(source=source, target=target, assessment=assessment,
                           strategies=strategies, strategy=chosen, options=options)
        selected = plan.option(chosen)
        if chosen != "none" and (selected is None or not selected.enabled):
            reason = selected.caution if selected else "It does not apply to this session."
            raise HandoffError(f"That transfer type is unavailable. {reason}")

        if assessment.peer_key:
            peer = sessions.get(assessment.peer_key)
            plan.dest_id = assessment.peer_key[1]
            plan.dest_path = str(peer.path) if peer and peer.path else ""
        self._workspace_report(plan, sessions)
        self._safety_checks(plan, sessions)
        if source.path is not None and Path(source.path).is_file():
            plan.source_state = scan.file_state(Path(source.path))
        # Only a strategy that writes into the existing peer depends on it staying put.
        if (chosen in ("append", "replace", "native_refresh", "delta_refresh")
                and plan.dest_path and Path(plan.dest_path).is_file()):
            plan.dest_state = scan.file_state(Path(plan.dest_path))
        if plan.can_execute:
            try:
                self._measure(plan, sessions)
            except (ci.ImportSkipped, ValueError, OSError, RuntimeError) as err:
                plan.blocked = f"This conversation cannot be converted: {err}"
        return plan

    def _codex_strategies(self, s: Session, a: Assessment, sessions: dict) -> list[StrategyOption]:
        state = a.state
        peer = sessions.get(a.peer_key) if a.peer_key else None
        new = StrategyOption("new_session", "Create a new Claude session",
                             "Convert this thread into a new Claude session with bounded context.")
        cont = StrategyOption("continuation", "Create a separate continuation session",
                              "A new Claude session that records its relationship to the existing "
                              "one. The existing session is not modified.")
        none = StrategyOption("none", "Don't transfer", "Nothing is written.")
        if state is SyncState.NOT_TRANSFERRED or state is SyncState.MISSING_DEST:
            new.recommended = True
            return [new, none]
        if state is SyncState.UNCHANGED:
            none.recommended = True
            new.label = "Create another copy anyway"
            new.caution = "This duplicates a conversation Claude already has."
            return [none, new]
        if state is SyncState.SOURCE_ADVANCED:
            append = StrategyOption(
                "append", "Append the new work to the Claude session",
                f"Adds only the turns after the last checkpoint to {a.peer_key[1][:8]}, "
                "after a backup. Nothing already in it changes.", recommended=True)
            options = [append, cont, none]
            if peer is not None and self._replaceable(peer):
                options.insert(1, StrategyOption(
                    "replace", "Rebuild the imported session in place",
                    "Regenerates the bounded view with the latest history, after a backup. Only "
                    "offered because Claude has not continued that session.",
                    caution="The previous copy moves to the backup folder."))
            return options
        if state is SyncState.DEST_ADVANCED:
            none.recommended = True
            cont.caution = "Claude has newer work than this thread, and this thread has nothing new."
            return [none, cont]
        append = StrategyOption("append", "Append to the Claude session", "", enabled=False,
                                caution="Disabled: the histories have diverged or changed, and "
                                        "appending would merge them silently.")
        cont.recommended = state is not SyncState.AMBIGUOUS
        return [cont, append, none]

    def _claude_strategies(self, s: Session, a: Assessment, sessions: dict) -> list[StrategyOption]:
        state = a.state
        none = StrategyOption("none", "Don't transfer", "Nothing is written.")
        cont = StrategyOption(
            "delta_import", "Create a separate Codex continuation thread",
            "A new Codex thread with a handoff note and the Claude work since the last common "
            "checkpoint. The existing thread is not modified.")
        full = StrategyOption(
            "delta_full", "Create a new Codex thread from the whole session",
            "Imports the full Claude transcript through a separate file. No session is modified.")
        if state is SyncState.NOT_TRANSFERRED:
            native = StrategyOption(
                "native_import", "Import with Codex's built-in importer",
                "Runs Codex's importer on exactly this session. Codex stores tool activity as "
                "text and does not keep tool results as structured data.", recommended=True)
            return [native, none]
        if state is SyncState.UNCHANGED:
            none.recommended = True
            return [none]
        if state is SyncState.MISSING_DEST:
            full.recommended = True
            return [full, none]
        link = a.link
        if state is SyncState.SOURCE_ADVANCED:
            peer = sessions.get(a.peer_key)
            if peer is not None and (peer.native_after_import or 0) > 0:
                # Codex's importer won't touch a thread with native work, even when that
                # work already came back here. A continuation carries only what's new.
                cont.label = "Create a Codex continuation with the new Claude work"
                cont.description = (f"Thread {a.peer_key[1][:13]} has its own turns, so Codex's "
                                    "importer can't add to it. A new thread gets a handoff note "
                                    "and only the Claude work since the last checkpoint.")
                cont.recommended = True
                return [cont, none]
            if link.delta_path:
                refresh = StrategyOption(
                    "delta_refresh", "Update the existing Codex thread",
                    f"Adds the new Claude turns to thread {a.peer_key[1][:13]} through Codex's "
                    "importer, which appends them.", recommended=True)
            elif link.relation in ("native_import",):
                refresh = StrategyOption(
                    "native_refresh", "Update the existing Codex thread",
                    f"Reruns Codex's importer, which appends the new turns to thread "
                    f"{a.peer_key[1][:13]}. Codex refuses if that thread has its own new work.",
                    recommended=True)
            else:
                # Claude session imported from Codex: only send what Codex doesn't have.
                cont.label = "Create a Codex continuation with the new Claude work"
                cont.recommended = True
                return [cont, none]
            return [refresh, cont, none]
        if state is SyncState.DEST_ADVANCED:
            none.recommended = True
            cont.enabled = False
            cont.caution = "This session has nothing new to send."
            return [none, cont]
        if state is SyncState.BOTH_ADVANCED:
            cont.recommended = True
            return [cont, none]
        # Modified source or ambiguous lineage: positions can't be trusted.
        full.label = "Create a new Codex thread from the current session"
        full.caution = "Earlier checkpoints can't be trusted, so everything is included."
        return [full, none]

    def _replaceable(self, peer: Session) -> bool:
        if peer.path is None or peer.active or not peer.imported or peer.native_after_import:
            return False
        try:
            info = self.discovery.claude_scan(Path(peer.path))
        except OSError:
            return False
        return bool(info.get("provenance")) and not info.get("native_assistant")

    def _workspace_report(self, plan: HandoffPlan, sessions: dict) -> None:
        source = plan.source
        ws = inspect_workspace(source.cwd, source.git_branch)
        plan.workspace = ws
        plan.workspace_notes.extend(ws.notes)
        dest_cwd = source.cwd
        if plan.assessment.peer_key:
            peer = sessions.get(plan.assessment.peer_key)
            dest_cwd = peer.cwd if peer and peer.cwd else source.cwd
        plan.workspace_notes.extend(compare_workspaces(ws, dest_cwd))

    def _safety_checks(self, plan: HandoffPlan, sessions: dict) -> None:
        source, assessment = plan.source, plan.assessment
        if source.lineage_error or source.raw.get("lineage_error"):
            err = source.lineage_error or source.raw.get("lineage_error")
            plan.blocked = f"This thread's full history can't be reconstructed: {err}"
            return
        if source.path is None or not Path(source.path).is_file():
            plan.blocked = "The source file is missing, so nothing can be transferred."
            return
        peer = sessions.get(assessment.peer_key) if assessment.peer_key else None
        if plan.strategy in ("append", "replace", "native_refresh", "delta_refresh") and peer is not None:
            if peer.active:
                plan.blocked = (f"The destination session is in use ({peer.active_reason}). "
                                "Close it first: changing an open session can lose work.")
                return
        if source.active:
            plan.warnings.append(f"The source session looks active ({source.active_reason}). Work "
                                 "after this point stays behind until the next handoff.")
        if source.provider is CODEX and source.path is not None:
            try:
                if self.discovery.rollout_scan(source).get("open_turn"):
                    plan.warnings.append("The last Codex turn never finished; a command or task "
                                         "may still be running or may have been cut off.")
            except (OSError, RuntimeError, ValueError):
                pass
        if source.recent and not source.active:
            plan.warnings.append("The source changed moments ago. If it is still working, "
                                 "that later work stays behind until the next handoff.")
        if source.provider is CLAUDE and source.path is not None:
            try:
                if self.discovery.claude_scan(Path(source.path)).get("open_tool_use"):
                    plan.warnings.append("The transcript ends with an unanswered tool call; a "
                                         "command may still be running or may have been cut off.")
            except OSError:
                pass
        if plan.target is CLAUDE and plan.strategy in ("append", "replace") and self.desktop_probe():
            plan.warnings.append("Claude Desktop is running. A session it has open keeps a cached "
                                 "copy; relaunch it before continuing this session.")
        if plan.options.summary_mode == "claude" and plan.strategy not in ("append", "native_import", "native_refresh"):
            plan.warnings.append(
                f"AI summary authorization required: this sends earlier transcript excerpts to "
                f"Claude using the {plan.options.summary_model} model, with up to "
                f"{plan.options.summary_max_calls} calls at ${plan.options.summary_max_usd:.2f} "
                "per call. That is a per-call limit, not a guaranteed total.")
        duplicate_of = source.duplicate_note
        if duplicate_of:
            plan.warnings.append(duplicate_of)

    # ------------------------------------------------------------------ measuring
    def _measure(self, plan: HandoffPlan, sessions: dict) -> None:
        strategy = plan.strategy
        if strategy in ("new_session", "continuation", "replace"):
            entry = None
            if strategy == "replace":
                entry = self._ledger_entry_for(plan)
            built = ci.build_import(plan.source.raw, plan.options.as_args(
                self.roots.claude_projects, dry_run=True), self.version_probe(),
                replace_entry=entry, extra_banner=self._continuation_note(plan))
            self._apply_built(plan, built)
            if strategy != "replace":
                # A new session's id is minted when it is written.
                plan.dest_id = ""
                plan.dest_path = str(self.roots.claude_projects / ci.slug_for(built["cwd"]))
        elif strategy == "append":
            records, problem = self._codex_new_records(plan)
            if problem:
                plan.blocked = problem
                return
            res = ci.append_codex_records(
                plan.source.raw, records, 0, Path(plan.dest_path), mode=self._append_mode(plan),
                keep_developer=plan.options.keep_developer, version=self.version_probe(),
                dry_run=True, transform=self._bounder(plan))
            if res["errs"]:
                plan.blocked = f"The new turns failed validation: {res['errs'][:3]}"
                return
            plan.stats = res.get("stats", {})
            plan.new_human = plan.stats.get("user", 0)
            plan.new_assistant = plan.stats.get("assistant", 0)
            plan.estimated_after = estimate_tokens(res["out"])
            plan.summary_note = "Only new turns are added; no summary is needed."
            self._check_budget_growth(plan)
        else:  # to Codex
            built = self._build_delta(plan, sessions, dry_run=True)
            plan.stats = built["stats"]
            plan.new_human = built["stats"]["human"]
            plan.new_assistant = built["stats"]["assistant"]
            plan.estimated_after = built["estimate"]
            plan.summary_note = built["summary_note"]
            plan.summary = built["summary"]

    def _apply_built(self, plan: HandoffPlan, built: dict) -> None:
        plan.stats = dict(built["conv"].stats)
        plan.new_human = plan.stats.get("user", 0)
        plan.new_assistant = plan.stats.get("assistant", 0)
        plan.budget = built["budget"]
        plan.summary = built["summary"]
        if built["budget"]:
            plan.estimated_before = built["budget"]["before"]
            plan.estimated_after = built["budget"]["after"]
        else:
            plan.estimated_after = estimate_tokens(built["out"])
            plan.estimated_before = plan.estimated_after
        s = built["summary"]
        if s:
            plan.summary_note = {
                "index": "Offline evidence index: quoted excerpts, not an AI summary.",
                "claude": "AI summary by Claude: fallible; citations are checked.",
                "off": "No historical note beyond the list of earlier requests.",
                "file": "Summary from a file you supplied.",
            }.get(s["mode"], "")
            if s.get("dry_run_skipped_claude"):
                plan.summary_note = ("Preview uses the offline index. The AI summary runs only "
                                     "after you confirm, so its final size may differ.")

    def _check_budget_growth(self, plan: HandoffPlan) -> None:
        budget = plan.options.context_budget
        if not budget or not plan.dest_path:
            return
        dest_size = Path(plan.dest_path).stat().st_size if Path(plan.dest_path).is_file() else 0
        # File size / 2 is the same conservative ratio as estimate_tokens uses per message.
        approx = dest_size // 2 + (plan.estimated_after or 0)
        if approx > budget:
            plan.warnings.append(f"The session may exceed the {budget:,}-token budget after this "
                                 f"append (very roughly {approx:,}, an overestimate). Claude will "
                                 "compact it, or use a new bounded session instead.")

    def _append_mode(self, plan: HandoffPlan) -> str:
        link = plan.assessment.link
        return (link.meta.get("mode") if link else None) or plan.options.mode

    def _bounder(self, plan: HandoffPlan):
        if plan.options.context_budget is None:
            return None
        limit = plan.options.max_tool_chars
        return lambda out: [bound_record(r, limit)[0] for r in out]

    def _continuation_note(self, plan: HandoffPlan) -> str | None:
        if plan.strategy != "continuation":
            return None
        a = plan.assessment
        peer = a.peer_key[1] if a.peer_key else "(unknown)"
        return (f"ContextBridge continuation session. It branches from Claude session {peer}; "
                f"that session was not modified and holds separate work. State at handoff: "
                f"{a.state.label}. {a.detail}")

    def _ledger_entry_for(self, plan: HandoffPlan) -> dict:
        entry = ci.load_ledger().get(plan.source.session_id)
        if entry and entry.get("claudeSessionId") == plan.dest_id:
            return entry
        return {"claudeSessionId": plan.dest_id, "transcript": plan.dest_path}

    def _codex_new_records(self, plan: HandoffPlan) -> tuple[list[tuple[int, dict]], str]:
        link = plan.assessment.link
        side = link.side(CODEX, plan.source.session_id)
        path, offset, sha = link.position(side)
        return codex_records_after(plan.source.raw, path, offset, sha)

    # ------------------------------------------------------------------ Claude -> delta
    def _delta_range(self, plan: HandoffPlan) -> tuple[int, str]:
        """(start offset, since-timestamp) of the Claude records Codex doesn't have."""
        if plan.strategy == "delta_full":
            return 0, ""
        link = plan.assessment.link
        if link is None:
            return 0, ""
        side = link.side(CLAUDE, plan.source.session_id)
        _, offset, _ = link.position(side)
        if offset is not None:
            return offset, ""
        from .lineage import _utc_marker
        return 0, _utc_marker(link.transferred_at)

    def _build_delta(self, plan: HandoffPlan, sessions: dict, dry_run: bool) -> dict:
        """The Claude records to import, plus a handoff note when they aren't the whole
        conversation. Native imports need no file of ours, but share the measurement."""
        source = plan.source
        path = Path(source.path)
        start, since = self._delta_range(plan)
        earlier: list[dict] = []
        chain: list[dict] = []
        end = 0
        for pos, rec in scan.iter_claude(path, 0):
            end = pos
            if rec.get("isSidechain") or rec.get("type") not in ("user", "assistant"):
                continue
            ts = rec.get("timestamp") or ""
            if pos <= start or (since and ts and ts <= since):
                earlier.append(rec)
            elif not rec.get("isMeta"):
                chain.append(rec)
        humans = sum(1 for r in chain if scan.human_text(r))
        assistants = sum(1 for r in chain if r.get("type") == "assistant"
                         and (r.get("message") or {}).get("model") not in scan.SYNTHETIC_MODELS)
        stats = {"human": humans, "assistant": assistants, "records": len(chain),
                 "earlier_records": len(earlier)}
        result = {"stats": stats, "end": end, "summary": None, "summary_note": "",
                  "estimate": estimate_tokens(chain), "records": chain, "note": ""}
        if plan.strategy in ("native_import", "native_refresh", "delta_refresh", "delta_full"):
            result["summary_note"] = "No summary is added: these records are imported as they are."
            return result
        note, summary = self._handoff_note(plan, earlier, sessions, dry_run)
        result["note"] = note
        result["summary"] = summary
        result["estimate"] += len(note.encode("utf-8")) // 2
        result["summary_note"] = {
            "index": "Earlier history is an offline evidence index: quoted excerpts, not an AI summary.",
            "claude": "Earlier history is an AI summary by Claude: fallible; citations are checked.",
            "off": "Earlier history is left out; the note only points to where it is.",
        }.get(summary["mode"], "")
        if summary.get("dry_run_skipped_claude"):
            result["summary_note"] = ("Preview uses the offline index. The AI summary runs only "
                                      "after you confirm, so its final size may differ.")
        return result

    def _handoff_note(self, plan: HandoffPlan, earlier: list[dict], sessions: dict,
                      dry_run: bool) -> tuple[str, dict]:
        source, options = plan.source, plan.options
        related = []
        for link in self.store.links_for(CLAUDE, source.session_id):
            provider, sid = link.peer_of(CLAUDE, source.session_id)
            related.append(f"- {provider.label} session {sid}")
        ws = plan.workspace
        workspace = ""
        if ws and ws.is_repo:
            workspace = (f"\nWorkspace at handoff (verify it; the transcript does not carry code): "
                         f"{ws.root} on {ws.branch} at {ws.head}, with {ws.modified} modified, "
                         f"{ws.staged} staged and {ws.untracked} untracked files.\n")
        carried = next((str((r.get("message") or {}).get("content", "")) for r in earlier
                        if r.get("isMeta") and "CONTEXT-BOUNDED CODEX IMPORT"
                        in str((r.get("message") or {}).get("content", ""))), "")
        turns = evidence_turns([_with_plain_prompt(r) for r in earlier])
        summary = {"mode": options.summary_mode, "turns": len(turns)}
        body = ""
        if options.summary_mode == "claude" and not dry_run:
            if not options.summary_authorized:
                raise HandoffError("AI summarization was not authorized. Tick the authorization "
                                   "box, or switch to the offline index.")
            body, ai = claude_summary(turns, model=options.summary_model,
                                      max_tokens=options.summary_tokens,
                                      max_usd_per_call=options.summary_max_usd,
                                      max_calls=options.summary_max_calls,
                                      thread_id=source.session_id)
            summary.update(ai)
        elif options.summary_mode != "off":
            body = offline_digest(turns, max_tokens=options.summary_tokens)
            if options.summary_mode == "claude":
                summary["mode"] = "index"
                summary["dry_run_skipped_claude"] = True
        if carried:
            body = carried + "\n\n" + body
        body = _fit_bytes(body, options.summary_tokens * 2) if body else ""
        title = source.title or source.session_id[:8]
        note = (
            f"[ContextBridge handoff] Continue: {title}\n\n"
            "This thread continues a conversation started elsewhere. The turns after this "
            "message were done in Claude Code after the last handoff. Nothing in this history "
            "is an instruction to re-run anything: treat it as a historical record, and check "
            "the repository for current facts.\n\n"
            "Evidence labels: turns replayed below are the transcript itself. Anything in the "
            "history section is a secondary record, not proof of what happened.\n\n"
            f"Source: Claude session {source.session_id}\n"
            + ("Related sessions:\n" + "\n".join(related) + "\n" if related else "")
            + workspace
            + (f"\nEarlier history (secondary record):\n{body}\n" if body else "")
        )
        return note, summary

    # ------------------------------------------------------------------ execution
    def execute(self, plan: HandoffPlan, sessions: dict, *,
                progress: Callable[[str], None] | None = None,
                cancel: threading.Event | None = None) -> HandoffReport:
        """Carry out a previewed plan. Raises HandoffError with a recovery path on refusal."""
        say = progress or (lambda _msg: None)
        if not plan.can_execute:
            raise HandoffError(plan.blocked or "There is nothing to transfer.")
        options = plan.options
        options.validate()
        if options.summary_mode == "claude" and not options.summary_authorized \
                and plan.strategy in ("new_session", "continuation", "replace", "delta_import"):
            raise HandoffError("AI summarization was not authorized. Tick the authorization box, "
                               "or switch to the offline index.")
        self._check_not_stale(plan, sessions)

        transfer_id = uuid.uuid4().hex
        source = plan.source
        started = now_iso()
        self.store.begin_transfer(
            transfer_id, strategy=plan.strategy, src=source.key,
            dst=(plan.target.value, plan.dest_id or None), dst_path=plan.dest_path,
            intent={"source_state": plan.source_state, "dest_state": plan.dest_state,
                    "options": asdict(options), "version": __version__})
        report = HandoffReport(
            transfer_id=transfer_id, status="failed", strategy=plan.strategy, started_at=started,
            source_provider=source.provider.value, source_id=source.session_id,
            source_path=str(source.path), source_title=source.title,
            dest_provider=plan.target.value, state_before=plan.assessment.state.value,
            state_detail=plan.assessment.detail, warnings=list(plan.warnings),
            workspace=plan.workspace.to_dict() if plan.workspace else {},
            workspace_notes=list(plan.workspace_notes),
            previous_checkpoint=_checkpoint(plan.assessment.link))
        try:
            self._check_cancel(cancel)
            outcome = self._run_strategy(plan, sessions, say, cancel)
        except HandoffCancelled as err:
            report.status = "cancelled"
            report.error = str(err)
            return self._finish(transfer_id, report)
        except (HandoffError, ci.ImportSkipped, OSError, RuntimeError, ValueError,
                CodexUnavailable, SystemExit) as err:
            report.error = str(err)
            log.exception("handoff failed")
            self._finish(transfer_id, report)
            raise HandoffError(f"{err}\n\nThe source was not changed. Details are in the report: "
                               f"{report.report_path}") from err
        self._apply_outcome(plan, outcome, report, transfer_id)
        return self._finish(transfer_id, report, backup=outcome.backup)

    def _check_cancel(self, cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise HandoffCancelled("Cancelled before anything was written.")

    def _check_not_stale(self, plan: HandoffPlan, sessions: dict) -> None:
        source = plan.source
        if source.path is not None and plan.source_state is not None:
            if scan.file_state(Path(source.path)) != plan.source_state:
                raise HandoffError("The source session changed since this preview. Preview again "
                                   "so the transfer covers its latest work.")
        if plan.dest_state is not None and plan.dest_path:
            if not Path(plan.dest_path).is_file() or scan.file_state(Path(plan.dest_path)) != plan.dest_state:
                raise HandoffError("The destination session changed since this preview. Nothing "
                                   "was written. Preview again to see how it moved.")
        peer = sessions.get(plan.assessment.peer_key) if plan.assessment.peer_key else None
        if plan.strategy in ("append", "replace") and peer is not None:
            if peer.key[1] in self.discovery.live_claude_sessions():
                raise HandoffError("The destination session is now running in Claude Code. "
                                   "Close it, then preview again.")

    def _run_strategy(self, plan, sessions, say, cancel) -> _Outcome:
        strategy = plan.strategy
        if strategy in ("new_session", "continuation"):
            return self._run_new_claude(plan, say, cancel)
        if strategy == "append":
            return self._run_append(plan, say, cancel)
        if strategy == "replace":
            return self._run_replace(plan, say, cancel)
        if strategy in ("native_import", "native_refresh"):
            return self._run_native(plan, sessions, say, cancel)
        if strategy in ("delta_import", "delta_full", "delta_refresh"):
            return self._run_delta(plan, sessions, say, cancel)
        raise HandoffError(f"Unknown transfer type: {strategy}")

    # -- Codex -> Claude -----------------------------------------------------------------
    def _run_new_claude(self, plan: HandoffPlan, say, cancel) -> _Outcome:
        s = plan.source.raw
        args = plan.options.as_args(self.roots.claude_projects, dry_run=False)
        say("Converting the conversation" + (" and summarizing earlier turns"
                                             if plan.options.summary_mode == "claude" else ""))
        built = ci.build_import(s, args, self.version_probe(),
                                extra_banner=self._continuation_note(plan))
        self._check_cancel(cancel)
        target = self.roots.claude_projects / ci.slug_for(built["cwd"]) / f"{built['conv'].session_id}.jsonl"
        if target.exists():
            raise HandoffError(f"Refusing to overwrite an existing transcript: {target}")
        self._backup_ledger()
        say("Writing the Claude transcript")
        ci.write_import(target, built["lines"])
        problems = self._validate_written(target, 0, None)
        if problems:
            self._quarantine(target)
            raise HandoffError(f"The written transcript failed validation: {problems[:3]}")
        card = self._register(plan, built, target)
        ledger = ci.load_ledger()
        ledger[s["id"]] = ci.ledger_entry(s, built, target, card, args)
        ci.save_ledger(ledger)
        return _Outcome(
            dest_id=built["conv"].session_id, dest_path=str(target),
            relation="continuation" if plan.strategy == "continuation" else "import",
            src_pos=(str(s["path"]), built["consumed"], built["digest"]),
            dst_pos=(str(target), target.stat().st_size, _sha(target)),
            stats=dict(built["conv"].stats), budget=built["budget"], summary=built["summary"],
            new_human=built["conv"].stats["user"], new_assistant=built["conv"].stats["assistant"],
            validation=["Transcript structure valid; every tool call has its result.",
                        "Parent chain unbroken across all records."], title=built["title"])

    def _run_append(self, plan: HandoffPlan, say, cancel) -> _Outcome:
        s = plan.source.raw
        target = Path(plan.dest_path)
        link = plan.assessment.link
        records, problem = self._codex_new_records(plan)
        if problem:
            raise HandoffError(problem)
        active = s["path"]
        data = ci.rollout_bytes(active)
        parsed = ci.records_from(data)
        consumed = parsed[-1][0] if parsed else 0
        digest = hashlib.sha256(data[:consumed]).hexdigest()
        self._check_cancel(cancel)
        size_before = target.stat().st_size
        native = not self.discovery.claude_scan(target).get("provenance")
        say("Appending the new turns")
        self._backup_ledger()
        res = ci.append_codex_records(
            s, records, 0, target, mode=self._append_mode(plan),
            keep_developer=plan.options.keep_developer, version=self.version_probe(),
            native=native, digest_of=lambda _end: digest, transform=self._bounder(plan))
        if res["errs"]:
            raise HandoffError(f"The new turns failed validation, nothing was appended: {res['errs'][:3]}")
        if not res["out"]:
            raise HandoffError("There were no new conversation records to append.")
        backup = res["backup"]
        problems = self._validate_written(target, size_before, res["tail"]["leaf"], backup)
        if problems:
            self._restore(target, backup, res)
            raise HandoffError(f"The append failed validation and was undone: {problems[:3]}")
        now = now_iso()
        ledger = ci.load_ledger()
        entry = ledger.setdefault(s["id"], {
            "claudeSessionId": res["sessionId"], "card": None, "cwd": res["tail"]["cwd"],
            "title": res["tail"]["title"] or s["first_user"], "importedAt": now,
            "kind": "import" if not native else "round-trip", "mode": self._append_mode(plan)})
        entry.update({"transcript": str(target), "syncedAt": now, "sourceRollout": str(active),
                      "sourceSha256": digest, "sourceBytes": consumed,
                      "sourceMtime": int(Path(active).stat().st_mtime),
                      "transcriptBytes": target.stat().st_size, "toolVersion": ci.TOOL_VERSION})
        # Only when appending to the session that ledger entry names; else keep it.
        if entry.get("claudeSessionId") == res["sessionId"]:
            ci.save_ledger(ledger)
        desktop_register.touch(res["sessionId"], ci.ms_of(res["last"]), res["stats"]["user"])
        return _Outcome(
            dest_id=plan.dest_id, dest_path=str(target), relation=link.relation,
            backup=str(backup), src_pos=(str(active), consumed, digest),
            dst_pos=(str(target), target.stat().st_size, _sha(target)), stats=dict(res["stats"]),
            new_human=res["stats"]["user"], new_assistant=res["stats"]["assistant"],
            validation=["Original transcript bytes verified unchanged before the new turns.",
                        "New records chain onto the session's last message.",
                        "Every new tool call has its result."],
            title=res["tail"]["title"] or plan.source.title)

    def _run_replace(self, plan: HandoffPlan, say, cancel) -> _Outcome:
        s = plan.source.raw
        target = Path(plan.dest_path)
        entry = self._ledger_entry_for(plan)
        ci.verify_replace_target(target, {**entry, "claudeSessionId": plan.dest_id},
                                 str(self.roots.claude_projects))
        say("Rebuilding the bounded session")
        args = plan.options.as_args(self.roots.claude_projects, dry_run=False)
        built = ci.build_import(s, args, self.version_probe(), replace_entry=entry)
        self._check_cancel(cancel)
        self._backup_ledger()
        backup = ci.write_import(target, built["lines"], replace=True)
        problems = self._validate_written(target, 0, None)
        if problems:
            ci.atomic_write_bytes(target, Path(backup).read_bytes())
            raise HandoffError(f"The rebuilt transcript failed validation and was restored: {problems[:3]}")
        ledger = ci.load_ledger()
        ledger[s["id"]] = ci.ledger_entry(s, built, target, None, args)
        ci.save_ledger(ledger)
        return _Outcome(
            dest_id=plan.dest_id, dest_path=str(target), relation="import", backup=str(backup),
            src_pos=(str(s["path"]), built["consumed"], built["digest"]),
            dst_pos=(str(target), target.stat().st_size, _sha(target)),
            stats=dict(built["conv"].stats), budget=built["budget"], summary=built["summary"],
            new_human=built["conv"].stats["user"], new_assistant=built["conv"].stats["assistant"],
            validation=["Rebuilt transcript structure valid."], title=built["title"])

    def _register(self, plan: HandoffPlan, built: dict, target: Path):
        if not plan.options.register_desktop:
            return None
        s = plan.source.raw
        return desktop_register.register(
            built["conv"].session_id, built["cwd"], built["title"], created_ms=ci.ms_of(s["ts"]),
            turns=built["conv"].stats["user"],
            provenance={"source": ci.IMPORT_MARK, "codexThreadId": s["id"],
                        "codexRollout": str(s["path"]), "importToolVersion": ci.TOOL_VERSION})

    # -- Claude -> Codex -----------------------------------------------------------------
    def _run_native(self, plan: HandoffPlan, sessions: dict, say, cancel) -> _Outcome:
        source = plan.source
        built = self._build_delta(plan, sessions, dry_run=False)
        path = Path(source.path)
        size_before = path.stat().st_size
        self._check_cancel(cancel)
        say("Running Codex's importer on this session")
        try:
            result = self.bridge_factory().import_claude_session(path, source.cwd, source.title)
        except CodexUnavailable as err:
            return self._assisted(plan, path, built, str(err))
        if result.status == "failed":
            raise HandoffError(f"Codex's importer reported a failure: {result.message}")
        if result.status == "no_change":
            raise HandoffError(result.message + " Nothing was changed in either application. If "
                               "that thread has its own work, send a continuation instead.")
        return self._codex_outcome(plan, result.thread_id, built, path, size_before,
                                   relation="native_import", delta_path="")

    def _run_delta(self, plan: HandoffPlan, sessions: dict, say, cancel) -> _Outcome:
        source = plan.source
        say("Preparing the Claude work for Codex")
        built = self._build_delta(plan, sessions, dry_run=False)
        if plan.strategy != "delta_full" and not built["stats"]["human"] and not built["stats"]["assistant"]:
            raise HandoffError("There is no new Claude work to send.")
        path = Path(source.path)
        size_before = path.stat().st_size
        self.roots.handoff_project.mkdir(parents=True, exist_ok=True)
        link = plan.assessment.link
        refreshing = plan.strategy == "delta_refresh" and link is not None and link.delta_path
        if refreshing:
            archived = Path(link.delta_path)
            if not archived.is_file():
                raise HandoffError(f"The handoff file for this thread is missing: {archived}. "
                                   "Use a continuation thread instead.")
            delta_file = self.roots.handoff_project / archived.name
            shutil.copy2(archived, delta_file)
            tail = ci.transcript_tail(delta_file)
            lines = self._delta_lines(built["records"], "", delta_file.stem, source, tail["leaf"], plan)
            ci.durable_append(delta_file, ci.jsonl_bytes(lines))
        else:
            delta_id = str(uuid.uuid4())
            delta_file = self.roots.handoff_project / f"{delta_id}.jsonl"
            lines = self._delta_lines(built["records"], built["note"], delta_id, source, None, plan)
            ci.atomic_write_bytes(delta_file, ci.jsonl_bytes(lines))
        self._check_cancel(cancel)
        say("Running Codex's importer on the handoff file")
        try:
            result = self.bridge_factory().import_claude_session(delta_file, source.cwd, source.title)
        except CodexUnavailable as err:
            return self._assisted(plan, delta_file, built, str(err))
        if result.status != "imported":
            self._file_away(delta_file, keep_archive=refreshing)
            raise HandoffError(f"Codex did not import the handoff: {result.message}")
        self.roots.deltas_dir.mkdir(parents=True, exist_ok=True)
        archived = self.roots.deltas_dir / delta_file.name
        delta_file.replace(archived)
        thread = result.thread_id or (plan.dest_id if refreshing else "")
        return self._codex_outcome(plan, thread, built, path, size_before,
                                   relation="delta_import", delta_path=str(archived))

    def _delta_lines(self, records: list[dict], note: str, delta_id: str, source: Session,
                     parent: str | None, plan: HandoffPlan) -> list[str]:
        """Records for Codex's importer: same text, renumbered onto a single chain. A
        tool's input and output are bounded when a context budget is set."""
        out: list[str] = []
        prev = parent
        first_ts = records[0].get("timestamp") if records else ci.iso(None)
        if note:
            prev = str(uuid.uuid4())
            out.append(json.dumps({
                "parentUuid": parent, "isSidechain": False, "userType": "external",
                "cwd": source.cwd, "sessionId": delta_id, "version": "contextbridge",
                "gitBranch": source.git_branch, "entrypoint": "contextbridge", "type": "user",
                "uuid": prev, "timestamp": first_ts,
                "message": {"role": "user", "content": note}}, ensure_ascii=False))
        limit = plan.options.max_tool_chars if plan.options.context_budget is not None else None
        for rec in records:
            copy = bound_record(rec, limit)[0] if limit else dict(rec)
            copy["sessionId"] = delta_id
            copy["parentUuid"] = prev
            prev = copy.get("uuid") or str(uuid.uuid4())
            copy["uuid"] = prev
            out.append(json.dumps(copy, ensure_ascii=False))
        return out

    def _assisted(self, plan: HandoffPlan, file: Path, built: dict, why: str) -> _Outcome:
        return _Outcome(
            dest_id="", dest_path="", relation="pending", status="assisted_pending",
            stats=built["stats"], new_human=built["stats"]["human"],
            new_assistant=built["stats"]["assistant"], src_pos=(str(plan.source.path),
                                                                built["end"], None),
            delta_path=str(file),
            follow_up=[f"Codex could not be reached automatically: {why}",
                       "Open Codex and use its import-from-Claude option. The file to import is "
                       f"waiting at: {file}",
                       "ContextBridge links the sessions automatically on its next refresh "
                       "after Codex records the import."],
            title=plan.source.title)

    def _codex_outcome(self, plan: HandoffPlan, thread_id: str | None, built: dict, path: Path,
                       size_before: int, *, relation: str, delta_path: str) -> _Outcome:
        if not thread_id:
            raise HandoffError("Codex finished the import without naming a thread.")
        rollouts = RolloutIndex(self.roots)
        files = rollouts.by_thread.get(thread_id) or []
        if not files:
            raise HandoffError(f"Codex reports thread {thread_id}, but its rollout was not found.")
        newest = max(files, key=lambda p: p.name)
        info = scan.scan_rollout_bytes(ci.rollout_bytes(newest))
        checks = []
        if info["import_end"] is None:
            raise HandoffError(f"Thread {thread_id} has no completed import marker, so the "
                               "transfer can't be verified. Check it in Codex before retrying.")
        checks.append("Codex recorded a completed import in the new thread's rollout.")
        expected = built["stats"]["human"]
        if info["human"] < expected:
            raise HandoffError(f"Thread {thread_id} holds {info['human']} human turn(s), fewer "
                               f"than the {expected} transferred. Inspect it before retrying.")
        checks.append(f"The thread holds at least the {expected} human turn(s) sent.")
        checks.append("Source transcript was not changed by the import.")
        src_offset = built["end"]
        return _Outcome(
            dest_id=thread_id, dest_path=str(newest), relation=relation,
            src_pos=(str(path), src_offset, scan.sha_prefix(path, src_offset)),
            dst_pos=(str(newest), info["import_end"], None), delta_path=delta_path,
            stats=built["stats"], new_human=built["stats"]["human"],
            new_assistant=built["stats"]["assistant"], summary=built["summary"], validation=checks,
            follow_up=["Open the thread in Codex to confirm it appears; the Codex app may need "
                       "a relaunch to show new imported threads."],
            title=plan.source.title)

    # -- durability ----------------------------------------------------------------------
    def _validate_written(self, target: Path, start: int, root: str | None,
                          backup: Path | None = None) -> list[str]:
        problems: list[str] = []
        if backup is not None:
            original = Path(backup).read_bytes()
            current = target.read_bytes()
            if not current.startswith(original):
                problems.append("existing transcript bytes changed")
        chain = []
        with target.open("rb") as fh:
            fh.seek(start)
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    problems.append("a written line is not valid JSON")
                    continue
                if rec.get("type") in ("user", "assistant"):
                    chain.append(rec)
        problems.extend(ci.validate(chain, root=root))
        return problems

    def _restore(self, target: Path, backup: Path | None, res: dict) -> None:
        if backup and Path(backup).is_file():
            ci.atomic_write_bytes(target, Path(backup).read_bytes())

    def _quarantine(self, target: Path) -> None:
        """A file this operation created that failed checks: kept aside, never deleted."""
        self.roots.backups_dir.mkdir(parents=True, exist_ok=True)
        target.replace(self.roots.backups_dir / f"failed-{_stamp()}-{target.name}")

    def _file_away(self, file: Path, keep_archive: bool) -> None:
        if not file.is_file():
            return
        if keep_archive:
            file.unlink()    # the archived copy stays authoritative; this is its working copy
        else:
            self._quarantine(file)

    def _backup_ledger(self) -> None:
        ledger = self.roots.claude_ledger
        if ledger.is_file():
            self.roots.backups_dir.mkdir(parents=True, exist_ok=True)
            ci.atomic_write_bytes(self.roots.backups_dir / f"codex-imports-{_stamp()}.json",
                                  ledger.read_bytes())

    def _apply_outcome(self, plan: HandoffPlan, outcome: _Outcome, report: HandoffReport,
                       transfer_id: str) -> None:
        source = plan.source
        if outcome.status == "committed":
            link = plan.assessment.link if plan.strategy in (
                "append", "replace", "native_refresh", "delta_refresh") else None
            if link is None:
                link = self.store.find_link((source.provider, source.session_id),
                                            (plan.target, outcome.dest_id))
            if link is None:
                link = Link(id=None, src_provider=source.provider, src_id=source.session_id,
                            dst_provider=plan.target, dst_id=outcome.dest_id,
                            relation=outcome.relation, origin="contextbridge")
            side = link.side(source.provider, source.session_id)
            other = "dst" if side == "src" else "src"
            link.set_position(side, *outcome.src_pos)
            link.set_position(other, *outcome.dst_pos)
            link.relation = link.relation if link.id else outcome.relation
            link.transferred_at = now_iso()
            link.origin = "contextbridge"
            if outcome.delta_path:
                link.delta_path = outcome.delta_path
            if outcome.budget or (link.bounded and plan.strategy == "append"):
                link.bounded = True
            link.meta.update({"mode": plan.options.mode, "contextBudget": plan.options.context_budget,
                              "summaryMode": plan.options.summary_mode})
            if plan.target is CODEX and not outcome.delta_path:
                # Codex's ledger entry for this import is already represented here.
                link.meta["codex_ledger_sha"] = _whole_file_sha(source.path)
            self.store.save_link(link)
            self.store.update_transfer(transfer_id, link_id=link.id, dst_id=outcome.dest_id)
        report.status = outcome.status
        report.dest_id = outcome.dest_id
        report.dest_path = outcome.dest_path
        report.new_checkpoint = {"source": _pos(outcome.src_pos), "destination": _pos(outcome.dst_pos)}
        report.relation = outcome.relation
        report.new_human = outcome.new_human
        report.new_assistant = outcome.new_assistant
        report.stats = outcome.stats
        report.budget = outcome.budget
        report.summary = outcome.summary
        report.summary_note = plan.summary_note
        report.backups = [outcome.backup] if outcome.backup else []
        report.delta_path = outcome.delta_path
        report.validation = outcome.validation
        report.follow_up = outcome.follow_up + [
            "A completed transfer moves conversation history only. It does not prove the task "
            "itself is finished or correctly resumed, and the destination must still have the "
            "right code, branch and dependencies."]
        report.title = outcome.title
        report.conversation_id = source.conversation_id

    def _finish(self, transfer_id: str, report: HandoffReport, backup: str = "") -> HandoffReport:
        report.finished_at = now_iso()
        path = write_report(self.roots.reports_dir, report)
        report.report_path = str(path)
        self.store.update_transfer(transfer_id, status=report.status, finished_at=report.finished_at,
                                   report_path=str(path), backup_path=backup or None,
                                   error=report.error or None, dst_id=report.dest_id or None,
                                   dst_path=report.dest_path or None)
        return report


def _with_plain_prompt(rec: dict) -> dict:
    """Native Claude human turns can be text-block lists; the evidence reader wants strings."""
    if rec.get("type") != "user":
        return rec
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list) and not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
        return {**rec, "message": {"role": "user", "content": text}}
    return rec


def _pos(pos: tuple) -> dict:
    return {"path": pos[0], "offset": pos[1], "sha256": pos[2]}


def _checkpoint(link: Link | None) -> dict:
    if link is None:
        return {}
    return {"source": {"session": link.src_id, "path": link.src_path, "offset": link.src_offset},
            "destination": {"session": link.dst_id, "path": link.dst_path, "offset": link.dst_offset},
            "transferred_at": link.transferred_at}


def _whole_file_sha(path) -> str | None:
    try:
        return _sha(Path(path))
    except OSError:
        return None
