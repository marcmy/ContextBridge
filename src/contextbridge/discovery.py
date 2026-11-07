"""Unified, read-only discovery of Codex threads and Claude sessions."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from . import converter

from . import scan
from .models import Provider, Session
from .paths import Roots
from .store import Store

log = logging.getLogger(__name__)
_UUID_JSONL = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.jsonl$", re.I)
ACTIVE_WINDOW_MS = 3 * 60 * 1000


def _clip_title(text: str, limit: int = 110) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def project_root(cwd: str, cache: dict[str, str] | None = None) -> str:
    """The enclosing repository, found without running git; else the directory itself.

    The filesystem is the only input: two conversations in one repository share a
    project, which says nothing about them being the same conversation.
    """
    cwd = converter.group_scratch(converter.normalize_path(cwd))
    if not cwd:
        return ""
    if cache is not None and cwd in cache:
        return cache[cwd]
    result = cwd
    try:
        current = Path(cwd)
        if current.is_dir():
            for candidate in (current, *current.parents):
                if (candidate / ".git").exists():
                    result = str(candidate)
                    break
                if candidate == Path.home():
                    break
    except OSError:
        pass
    if cache is not None:
        cache[cwd] = result
    return result


class RolloutIndex:
    """Rollout id -> header summary, read lazily so that resolving one thread's revision
    chain never decodes the hundreds of unrelated compressed rollouts beside it."""

    def __init__(self, roots: Roots):
        self.paths: dict[str, Path] = {}
        self.by_thread: dict[str, list[Path]] = {}
        self._summaries: dict[str, dict | None] = {}
        for base in (roots.codex_sessions, roots.codex_archived):
            if not base.is_dir():
                continue
            for dirpath, _dirs, names in os.walk(base):
                for name in names:
                    match = converter.ROLLOUT_RE.match(name)
                    if not match:
                        continue
                    full = Path(dirpath) / name
                    rid = match.group(2) or match.group(1)
                    # A rollout briefly exists both plain and compressed: prefer plain.
                    if rid not in self.paths or name.endswith(".jsonl"):
                        self.paths[rid] = full
                    self.by_thread.setdefault(match.group(1), [])
                    if not any(converter.same_rollout(p, full) for p in self.by_thread[match.group(1)]):
                        self.by_thread[match.group(1)].append(full)

    def get(self, rollout_id):
        if rollout_id in self._summaries:
            return self._summaries[rollout_id]
        path = self.paths.get(rollout_id)
        summary = None
        if path:
            try:
                summary = converter.summarize(path)
            except (OSError, EOFError, ValueError, RuntimeError) as err:
                log.warning("unreadable rollout %s: %s", path, err)
        self._summaries[rollout_id] = summary
        return summary


class Discovery:
    def __init__(self, roots: Roots, store: Store):
        self.roots = roots
        self.store = store
        self._projects: dict[str, str] = {}
        self.rollouts: RolloutIndex | None = None

    # -- Codex ------------------------------------------------------------------------
    def _state_rows(self) -> dict[str, dict]:
        db = self.roots.codex_state_db
        if not db.is_file():
            return {}
        try:
            conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute("SELECT * FROM threads").fetchall()
            finally:
                conn.close()
        except sqlite3.Error as err:
            log.warning("Codex state DB unreadable, falling back to rollouts: %s", err)
            return {}
        return {r["id"]: dict(r) for r in rows}

    def codex_sessions(self, include_internal: bool = False) -> list[Session]:
        self.rollouts = RolloutIndex(self.roots)
        state = self._state_rows()
        out: list[Session] = []
        seen: set[str] = set()
        for tid, row in state.items():
            path = converter.locate_rollout(row.get("rollout_path") or "")
            internal = converter.is_internal_session(
                {"source": row.get("source"), "thread_source": row.get("thread_source")})
            if not path.is_file():
                # A stale index row with nothing behind it is not a conversation.
                continue
            seen.add(tid)
            if internal and not include_internal:
                continue
            out.append(self._codex_from_state(tid, row, path, internal))
        for tid, paths in self.rollouts.by_thread.items():
            if tid in seen:
                continue
            session = self._codex_from_files(tid, paths)
            if session and (include_internal or not session.internal):
                out.append(session)
        return out

    def _codex_raw(self, tid: str, path: Path, row: dict | None) -> dict:
        summary = converter.summarize(path) or {}
        raw = {"path": path, "id": tid, "rollout_id": converter.rollout_id(path),
               "history_base": summary.get("history_base"),
               "forked_from": summary.get("forked_from") or "",
               "cwd": converter.normalize_path(summary.get("cwd") or (row or {}).get("cwd") or ""),
               "ts": summary.get("ts") or "", "originator": summary.get("originator") or "",
               "first_user": summary.get("first_user") or "", "lines": summary.get("lines"),
               "internal": bool(summary.get("internal")), "round_trip": ""}
        converter.resolve_lineage(raw, self.rollouts)
        return raw

    def _codex_from_state(self, tid: str, row: dict, path: Path, internal: bool) -> Session:
        title = row.get("name") or row.get("title") or row.get("first_user_message") or ""
        cwd = converter.normalize_path(row.get("cwd") or "")
        return Session(
            provider=Provider.CODEX, session_id=tid, title=_clip_title(readable_title(title) or title),
            cwd=cwd, project=project_root(cwd, self._projects), path=path,
            created_ms=row.get("created_at_ms") or (row.get("created_at") or 0) * 1000,
            updated_ms=row.get("updated_at_ms") or (row.get("updated_at") or 0) * 1000,
            archived=bool(row.get("archived")), internal=internal,
            git_branch=row.get("git_branch") or "", size_bytes=_size(path),
            source=row.get("originator") or row.get("source") or "",
            model=row.get("model") or "", tokens_used=row.get("tokens_used") or 0,
            revisions=len((self.rollouts.by_thread if self.rollouts else {}).get(tid, [path])),
        )

    def _codex_from_files(self, tid: str, paths: list[Path]) -> Session | None:
        newest = max(paths, key=lambda p: p.name)
        try:
            summary = converter.summarize(newest)
        except (OSError, EOFError, ValueError, RuntimeError) as err:
            log.warning("unreadable rollout %s: %s", newest, err)
            return None
        if not summary or not summary.get("id"):
            return None
        cwd = converter.normalize_path(summary.get("cwd") or "")
        archived = self.roots.codex_archived in newest.parents
        return Session(
            provider=Provider.CODEX, session_id=tid,
            title=_clip_title(summary.get("first_user") or "(untitled Codex thread)"),
            cwd=cwd, project=project_root(cwd, self._projects), path=newest,
            created_ms=scan.ms_of_iso(summary.get("ts")),
            updated_ms=int(newest.stat().st_mtime * 1000), archived=archived,
            internal=bool(summary.get("internal")), size_bytes=_size(newest),
            source=summary.get("originator") or "", revisions=len(paths),
        )

    def hydrate_codex(self, session: Session) -> Session:
        """Load the converter's view of a thread: revision chain and header fields."""
        if session.raw or session.path is None:
            return session
        if self.rollouts is None:
            self.rollouts = RolloutIndex(self.roots)
        session.raw = self._codex_raw(session.session_id, session.path, None)
        session.raw["cwd"] = session.cwd or session.raw["cwd"]
        session.raw["first_user"] = session.raw["first_user"] or session.title
        session.lineage_error = session.raw.get("lineage_error", "")
        return session

    def rollout_scan(self, session: Session) -> dict:
        """Work counts for the whole thread across its revision chain, cached by the
        active rollout's stat (ancestors are immutable)."""
        path = Path(session.path)
        size, mtime = scan.file_state(path)
        kind = f"rollout-{scan.SCANNER_VERSION}"
        cached = self.store.cached_scan(str(path), kind, size, mtime)
        if cached:
            return cached
        self.hydrate_codex(session)
        chain = session.raw.get("lineage") or [(path, None)]
        total = {"import_end": None, "import_path": "", "human": 0, "human_after_import": 0,
                 "assistant": 0, "assistant_after_import": 0, "first_ts": "", "last_ts": "",
                 "open_turn": False, "end": 0, "first_human": "",
                 "forked_from": session.raw.get("forked_from") or ""}
        prompts: list[str] = []
        for rollout, cutoff in chain:
            data = converter.rollout_bytes(rollout)
            part = scan.scan_rollout_bytes(data[:cutoff] if cutoff is not None else data)
            total["human"] += part["human"]
            total["assistant"] += part["assistant"]
            if part["import_end"] is not None:
                total["import_end"] = part["import_end"]
                total["import_path"] = str(rollout)
                total["human_after_import"] = part["human_after_import"]
                total["assistant_after_import"] = part["assistant_after_import"]
            else:
                total["human_after_import"] += part["human_after_import"]
                total["assistant_after_import"] += part["assistant_after_import"]
            total["first_ts"] = total["first_ts"] or part["first_ts"]
            total["last_ts"] = part["last_ts"] or total["last_ts"]
            total["open_turn"] = part["open_turn"]
            total["end"] = part["end"]
            total["first_human"] = total["first_human"] or part["first_human"]
            prompts.extend(part["prompts"])
        total["fingerprint"] = scan.fingerprint_of(prompts)
        self.store.put_scan(str(path), kind, size, mtime, total)
        return total

    # -- Claude -----------------------------------------------------------------------
    def claude_files(self) -> list[Path]:
        root = self.roots.claude_projects
        if not root.is_dir():
            return []
        out = []
        for slug in root.iterdir():
            if not slug.is_dir() or slug == self.roots.handoff_project:
                continue
            for entry in slug.iterdir():
                if entry.is_file() and _UUID_JSONL.match(entry.name):
                    out.append(entry)
        return out

    def claude_scan(self, path: Path) -> dict:
        size, mtime = scan.file_state(path)
        kind = f"claude-{scan.SCANNER_VERSION}"
        cached = self.store.cached_scan(str(path), kind, size, mtime)
        if cached:
            return cached
        info = scan.scan_claude(path)
        self.store.put_scan(str(path), kind, size, mtime, info)
        return info

    def claude_sessions(self, progress=None) -> list[Session]:
        files = self.claude_files()
        live = self.live_claude_sessions()
        out: list[Session] = []
        for i, path in enumerate(files):
            if progress and i % 8 == 0:
                progress(f"Reading Claude transcripts ({i}/{len(files)})")
            try:
                info = self.claude_scan(path)
            except OSError as err:
                log.warning("unreadable transcript %s: %s", path, err)
                continue
            if not info["human"] and not info["native_assistant"] and not info["imported_assistant"]:
                continue        # no conversation in it: an empty or tool-only shell
            cwd = converter.normalize_path(info["cwd"])
            st = path.stat()
            updated = scan.ms_of_iso(info["last_ts"]) or int(st.st_mtime * 1000)
            active = path.stem in live
            prov = info.get("provenance") or {}
            session = Session(
                provider=Provider.CLAUDE, session_id=path.stem,
                title=_clip_title(_claude_title(info) or "(untitled Claude session)"),
                cwd=cwd, project=project_root(cwd, self._projects), path=path,
                created_ms=scan.ms_of_iso(info["first_ts"]), updated_ms=updated,
                git_branch=info["git_branch"] if info["git_branch"] != "HEAD" else "",
                size_bytes=st.st_size, source=info["entrypoint"], model=info["model"],
                human_turns=info["human"], native_after_import=info["native_after_mark"],
                fingerprint=info["fingerprint"], imported=bool(prov.get("codexThreadId")),
                forked_from=(info.get("first_session_id")
                             if info.get("first_session_id") not in ("", path.stem) else ""),
                active=active, active_reason="running in Claude Code" if active else "",
            )
            out.append(session)
        return out

    def live_claude_sessions(self) -> set[str]:
        """Claude Code's own registry of running sessions, checked against live pids."""
        directory = self.roots.claude_live
        ids: set[str] = set()
        if not directory.is_dir():
            return ids
        for card in directory.glob("*.json"):
            try:
                data = json.loads(card.read_text(encoding="utf-8"))
                if data.get("sessionId") and _pid_alive(int(data.get("pid", 0))):
                    ids.add(data["sessionId"])
            except (OSError, ValueError):
                continue
        return ids

    def mark_recent(self, sessions: list[Session]) -> None:
        """Written moments ago. Switching agents right after a usage limit is the normal
        case, so this alone warns and protects cleanup, but never blocks a handoff."""
        now = time.time() * 1000
        for s in sessions:
            if s.updated_ms and now - s.updated_ms < ACTIVE_WINDOW_MS:
                s.recent = True


_MARKUP = re.compile(r"<[^>\n]{1,300}>")


def readable_title(text: str) -> str:
    """Pasted-attachment markup says nothing about what a conversation is about."""
    return converter.clean_title(_MARKUP.sub(" ", text or "")) or _MARKUP.sub(" ", text or "").strip()


def _claude_title(info: dict) -> str:
    title = info.get("custom_title") or ""
    if title and not title.lstrip().startswith("<"):
        return title
    return readable_title(info.get("first_human") or title)


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259            # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
