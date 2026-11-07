"""Conversation lineage: reconcile native ledgers into links, judge each link's state,
and find duplicates from provenance.

Three facts drive everything here:

* A *link* is a sync point. Each side records the byte position in its own file up to
  which the two sessions are known to agree, so "new work" is whatever follows.
* Ledgers are inputs. They are read, never rewritten, by this module.
* Titles and paths never establish identity. Only recorded provenance does; content
  overlap is reported as merely "possible".
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path

from . import converter

from . import scan
from .discovery import Discovery
from .models import Assessment, Delta, DuplicateCandidate, Link, Provider, Session, SyncState
from .paths import Roots
from .store import Store, now_iso

log = logging.getLogger(__name__)
CODEX = Provider.CODEX
CLAUDE = Provider.CLAUDE


class Lineage:
    def __init__(self, roots: Roots, store: Store, discovery: Discovery):
        self.roots = roots
        self.store = store
        self.discovery = discovery

    # -- reconcile ------------------------------------------------------------------------
    def reconcile(self, sessions: list[Session]) -> None:
        """Bring links up to date with what the native ledgers and transcripts record."""
        by_key = {s.key: s for s in sessions}
        self._from_claude_ledger(by_key)
        self._from_transcript_marks(by_key)
        self._from_codex_ledger(by_key)
        self._assign_conversations(by_key)

    def _load_json(self, path: Path) -> dict:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as err:
            log.warning("cannot read ledger %s: %s", path, err)
            return {}

    def _from_claude_ledger(self, by_key: dict) -> None:
        for tid, entry in self._load_json(self.roots.claude_ledger).items():
            if not isinstance(entry, dict) or not entry.get("claudeSessionId"):
                continue
            target = entry.get("transcript") or ""
            sid = entry["claudeSessionId"]
            when = entry.get("syncedAt") or entry.get("importedAt") or ""
            self._merge_ledger_link(
                codex_id=tid, claude_id=sid, direction="codex->claude",
                relation="round-trip" if entry.get("kind") == "round-trip" else "import",
                origin="claude-ledger", when=when,
                codex_pos=(converter.normalize_path(entry.get("sourceRollout")),
                           entry.get("sourceBytes"), entry.get("sourceSha256")),
                claude_path=target, bounded=bool(entry.get("contextBudget")),
                meta={k: entry.get(k) for k in ("mode", "contextBudget", "summaryMode")},
                by_key=by_key)

    def _from_transcript_marks(self, by_key: dict) -> None:
        """Provenance stamped in a transcript survives the ledger being lost or
        overwritten (a forced re-import replaces the ledger entry, not the old file)."""
        for path in self.discovery.claude_files():
            try:
                info = self.discovery.claude_scan(path)
            except OSError:
                continue
            prov = info.get("provenance") or {}
            tid = prov.get("codexThreadId")
            if not tid or self.store.find_link((CODEX, tid), (CLAUDE, path.stem)):
                continue
            self.store.save_link(Link(
                id=None, src_provider=CODEX, src_id=tid, dst_provider=CLAUDE, dst_id=path.stem,
                relation="import", origin="transcript-marker",
                transferred_at=prov.get("syncedAt") or prov.get("importedAt") or now_iso(),
                src_path=converter.normalize_path(prov.get("codexRollout")),
                src_sha=prov.get("codexRolloutSha256"), dst_path=str(path),
                dst_offset=info.get("mark_end")))

    def _from_codex_ledger(self, by_key: dict) -> None:
        data = self._load_json(self.roots.codex_ledger)
        delta_stems = {Path(l.delta_path).stem for l in self.store.links() if l.delta_path}
        for rec in data.get("records", []) if isinstance(data, dict) else []:
            thread = rec.get("imported_thread_id")
            source = converter.normalize_path(rec.get("source_path"))
            if not thread or not source:
                continue
            stem = Path(source).stem
            if stem in delta_stems:
                continue    # A delta file stands for the session it was cut from.
            when = _epoch_iso(rec.get("imported_at"))
            link = self.store.find_link((CLAUDE, stem), (CODEX, thread))
            if link and link.meta.get("codex_ledger_sha") == rec.get("content_sha256"):
                continue    # Already applied; don't re-derive positions.
            offset = _offset_of_sha(Path(source), rec.get("content_sha256")) \
                if Path(source).is_file() else None
            rollout = by_key.get(("codex", thread))
            import_end, rollout_path = None, ""
            if rollout is not None and rollout.path is not None:
                rollout_path = str(rollout.path)
                try:
                    info = self.discovery.rollout_scan(rollout)
                    import_end = info["import_end"]
                    rollout_path = info.get("import_path") or rollout_path
                except (OSError, RuntimeError, ValueError) as err:
                    log.warning("cannot scan %s: %s", rollout.path, err)
            if link is None:
                link = Link(id=None, src_provider=CLAUDE, src_id=stem, dst_provider=CODEX,
                            dst_id=thread, relation="native_import", origin="codex-ledger",
                            transferred_at=when)
            elif when and when <= (link.transferred_at or ""):
                link.meta["codex_ledger_sha"] = rec.get("content_sha256")
                self.store.save_link(link)
                continue
            side = link.side(CLAUDE, stem)
            link.set_position(side, source, offset, rec.get("content_sha256") if offset else None)
            link.set_position("dst" if side == "src" else "src", rollout_path, import_end, None)
            link.meta["codex_ledger_sha"] = rec.get("content_sha256")
            link.meta["source_modified_at"] = rec.get("source_modified_at")
            link.transferred_at = when or link.transferred_at
            self.store.save_link(link)

    def _merge_ledger_link(self, *, codex_id: str, claude_id: str, direction: str, relation: str,
                           origin: str, when: str, codex_pos: tuple, claude_path: str,
                           bounded: bool, meta: dict, by_key: dict) -> None:
        link = self.store.find_link((CODEX, codex_id), (CLAUDE, claude_id))
        if link is None:
            link = Link(id=None, src_provider=CODEX, src_id=codex_id, dst_provider=CLAUDE,
                        dst_id=claude_id, relation=relation, origin=origin, transferred_at=when,
                        bounded=bounded, meta=meta)
            link.set_position("src", *codex_pos)
            link.dst_path = claude_path
            link.dst_offset = self._claude_mark(claude_path)
            self.store.save_link(link)
            return
        # The CLI may have moved on after ContextBridge last looked at this pair.
        if when and when > (link.transferred_at or ""):
            side = link.side(CODEX, codex_id)
            link.set_position(side, *codex_pos)
            other = "dst" if side == "src" else "src"
            mark = self._claude_mark(claude_path)
            if mark is not None:
                link.set_position(other, claude_path, mark, None)
            link.transferred_at = when
            link.bounded = bounded or link.bounded
            self.store.save_link(link)

    def _claude_mark(self, claude_path: str) -> int | None:
        path = Path(claude_path)
        if not claude_path or not path.is_file():
            return None
        try:
            return self.discovery.claude_scan(path).get("mark_end")
        except OSError:
            return None

    def _assign_conversations(self, by_key: dict) -> None:
        """Connected sessions share one logical conversation; ids stay stable over runs."""
        parent: dict[tuple, tuple] = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        links = self.store.links()
        for link in links:
            parent[find((link.src_provider.value, link.src_id))] = find(
                (link.dst_provider.value, link.dst_id))
        groups: dict[tuple, list[tuple]] = defaultdict(list)
        for key, s in by_key.items():
            # Internal agent threads aren't conversations unless a link makes them one.
            if not s.internal or key in parent:
                groups[find(key)].append(key)
        for link in links:
            for key in ((link.src_provider.value, link.src_id),
                        (link.dst_provider.value, link.dst_id)):
                if key not in groups[find(key)]:
                    groups[find(key)].append(key)
        origin_time = {}
        for link in links:
            origin_time.setdefault((link.src_provider.value, link.src_id), link.transferred_at)
        rows: list[tuple] = []
        for members in groups.values():
            existing = sorted({c for c in (self.store.conversation_of(*k) for k in members) if c})
            sources = [k for k in members if k in origin_time and not any(
                (l.dst_provider.value, l.dst_id) == k for l in links)]
            origin = min(sources, key=lambda k: origin_time[k]) if sources else members[0]
            head = by_key.get(origin)
            cid = self.store.ensure_conversation(
                existing[0] if existing else None,
                title=head.title if head else "", origin_provider=origin[0],
                origin_session=origin[1])
            for key in members:
                s = by_key.get(key)
                if s is not None:
                    s.conversation_id = cid
                    s.origin_provider = Provider(origin[0])
                    rows.append((key[0], key[1], cid, str(s.path or ""), s.cwd, s.title,
                                 s.size_bytes, s.fingerprint))
        self.store.upsert_sessions(rows)

    # -- judging ---------------------------------------------------------------------------
    def side_delta(self, session: Session | None, link: Link, side: str) -> Delta:
        """How far this side has moved past its recorded position."""
        path, offset, sha = link.position(side)
        provider = link.src_provider if side == "src" else link.dst_provider
        if session is None or session.path is None or not Path(session.path).is_file():
            return Delta(missing=True, reason=f"{provider.label} session file not found")
        anchor = Path(session.path)
        size, mtime = scan.file_state(anchor)
        kind = f"delta-{scan.SCANNER_VERSION}:{path}:{offset}:{sha}:{link.transferred_at}"
        cached = self.store.cached_scan(str(anchor), kind, size, mtime)
        if cached is not None:
            return Delta(**cached)
        if provider is CODEX:
            delta = self._codex_delta(session, path, offset, sha)
        else:
            delta = self._claude_delta(anchor, offset, sha, link)
        self.store.put_scan(str(anchor), kind, size, mtime, asdict(delta))
        return delta

    def _claude_delta(self, path: Path, offset: int | None, sha: str | None, link: Link) -> Delta:
        size = path.stat().st_size
        if offset is None:
            # No byte position (older records): fall back to transcript timestamps.
            info = scan.scan_claude(path, 0, since_ts=_utc_marker(link.transferred_at))
            return Delta(known=False, human=info["native_human"],
                         assistant=info["native_assistant"], last_ts=info["last_ts"],
                         reason="position unknown; judged by timestamps after the transfer")
        if offset > size:
            return Delta(modified=True, reason="file is shorter than the recorded checkpoint")
        if sha and scan.sha_prefix(path, offset) != sha:
            return Delta(modified=True, reason="earlier content changed since the checkpoint")
        if offset == size:
            return Delta()
        info = scan.scan_claude(path, offset)
        return Delta(human=info["native_human"], assistant=info["native_assistant"],
                     last_ts=info["last_ts"])

    def _codex_delta(self, session: Session, path: str, offset: int | None, sha: str | None) -> Delta:
        self.discovery.hydrate_codex(session)
        if session.raw.get("lineage_error"):
            return Delta(known=False, reason=session.raw["lineage_error"])
        try:
            records, problem = codex_records_after(session.raw, path, offset, sha)
        except (OSError, ValueError, RuntimeError) as err:
            return Delta(known=False, reason=str(err))
        if problem:
            return Delta(modified=True, reason=problem)
        human = assistant = 0
        last = ""
        for _, rec in records:
            payload = rec.get("payload") or {}
            if rec.get("type") != "response_item" or not isinstance(payload, dict):
                continue
            if payload.get("type") == "message":
                if payload.get("role") == "user":
                    if converter.split_user_message(converter.text_of(payload.get("content")))[1]:
                        human += 1
                elif payload.get("role") == "assistant":
                    assistant += 1
                last = rec.get("timestamp") or last
            elif payload.get("type") in converter.CONTENT_TYPES:
                last = rec.get("timestamp") or last
        return Delta(human=human, assistant=assistant, last_ts=last)

    def assess(self, session: Session, sessions: dict, peer_provider: Provider | None = None) -> Assessment:
        """The state of `session` relative to the other provider, from its latest link."""
        target = peer_provider or session.provider.other
        links = [l for l in self.store.links_for(session.provider, session.session_id)
                 if l.peer_of(session.provider, session.session_id)[0] is target]
        if not links:
            return Assessment(None, SyncState.NOT_TRANSFERRED,
                              f"Never transferred to {target.label}.")
        link = links[0]
        result = self._assess_link(session, link, sessions)
        # Older links to other sessions of the target provider can mean an unmerged branch.
        extra = []
        for other in links[1:]:
            peer = other.peer_of(session.provider, session.session_id)
            peer_session = sessions.get((peer[0].value, peer[1]))
            if peer_session is None:
                continue
            side = "dst" if other.side(session.provider, session.session_id) == "src" else "src"
            if self.side_delta(peer_session, other, side).advanced:
                extra.append(peer[1][:13])
        if extra:
            result.detail += " Other linked sessions may hold separate work: " + ", ".join(extra) + "."
        return result

    def _assess_link(self, session: Session, link: Link, sessions: dict) -> Assessment:
        side = link.side(session.provider, session.session_id)
        peer_provider, peer_id = link.peer_of(session.provider, session.session_id)
        peer = sessions.get((peer_provider.value, peer_id))
        other = "dst" if side == "src" else "src"
        local = self.side_delta(session, link, side)
        remote = self.side_delta(peer, link, other)
        name = peer_provider.label
        key = (peer_provider.value, peer_id)

        def verdict(state, detail):
            return Assessment(link, state, detail, local, remote, key)

        if remote.missing:
            return verdict(SyncState.MISSING_DEST,
                           f"The linked {name} session {peer_id[:13]} is gone. A new transfer "
                           "would create a fresh session; the source is untouched.")
        if local.modified:
            return verdict(SyncState.MODIFIED_SOURCE,
                           f"This session changed after its last transfer: {local.reason}. "
                           "Appending could corrupt history, so only a separate continuation is offered.")
        if remote.modified:
            return verdict(SyncState.AMBIGUOUS,
                           f"The linked {name} session changed unexpectedly: {remote.reason}. "
                           "Automatic transfer is disabled until you choose a continuation.")
        if local.advanced and remote.advanced:
            return verdict(SyncState.BOTH_ADVANCED,
                           f"Both agents moved on since the last common checkpoint "
                           f"({local.human} new turn(s) here, {remote.human} in {name}). "
                           "A separate continuation session is recommended; nothing merges unless you choose it.")
        if local.advanced:
            return verdict(SyncState.SOURCE_ADVANCED,
                           f"{local.human} new human turn(s) and {local.assistant} assistant "
                           f"message(s) here are not in {name} yet.")
        if remote.advanced:
            return verdict(SyncState.DEST_ADVANCED,
                           f"{name} has {remote.human} newer human turn(s) that are not here. "
                           f"Open that session, or send it back from {name}.")
        notes = [d.reason for d in (local, remote) if d.reason]
        return verdict(SyncState.UNCHANGED,
                       f"Nothing new on either side since the last transfer to {name}."
                       + (f" ({'; '.join(notes)})" if notes else ""))

    # -- duplicates ------------------------------------------------------------------------
    def duplicates(self, sessions: dict) -> list[DuplicateCandidate]:
        out: list[DuplicateCandidate] = []
        claude_of_thread: dict[str, list[Session]] = defaultdict(list)
        links = self.store.links()

        for s in sessions.values():
            if s.provider is CLAUDE and s.path is not None:
                prov = self.discovery.claude_scan(Path(s.path)).get("provenance") or {}
                if prov.get("codexThreadId"):
                    claude_of_thread[prov["codexThreadId"]].append(s)

        # Imports of Codex's internal agents (from before they were filtered out).
        for link in links:
            if link.src_provider is not CODEX or link.dst_provider is not CLAUDE:
                continue
            origin = sessions.get(("codex", link.src_id))
            copy = sessions.get(("claude", link.dst_id))
            if origin is None or copy is None or not origin.internal:
                continue
            native = copy.native_after_import or 0
            reason = (f"Imported from an internal Codex agent thread ({link.src_id[:13]}), not "
                      "from a conversation you had.")
            reason += (f" It has {native} native turn(s), so it is protected." if native else "")
            out.append(DuplicateCandidate("internal_import", copy.key, None, "provenance",
                                          bool(native),
                                          not native and not copy.active and not copy.recent, reason))

        # Circular: Codex A -> Claude B -> Codex T, where Codex re-imported our import.
        for link in links:
            if link.relation != "native_import":
                continue
            claude = sessions.get(("claude", link.src_id))
            thread = sessions.get(("codex", link.dst_id))
            if claude is None or thread is None or claude.path is None:
                continue
            prov = self.discovery.claude_scan(Path(claude.path)).get("provenance") or {}
            origin = prov.get("codexThreadId")
            if not origin or origin == thread.session_id:
                continue
            native = self.native_work_codex(thread)
            canonical = sessions.get(("codex", origin))
            allowed = (bool(canonical) and not native and not thread.active
                       and not thread.recent and not thread.archived)
            reason = (f"Codex imported Claude session {claude.short_id}, which was itself "
                      f"exported from Codex thread {origin[:13]}.")
            if native:
                reason += f" It has {native} turn(s) of native Codex work, so it is protected."
            elif not canonical:
                reason += " The original thread is missing, so this copy is kept."
            elif thread.active or thread.recent:
                reason += " It looks active, so it is protected for now."
            out.append(DuplicateCandidate("circular_copy", thread.key,
                                          ("codex", origin) if canonical else None,
                                          "provenance", bool(native), allowed, reason))

        # Repeat imports: several Claude copies of one Codex thread (`import --force`).
        for tid, copies in claude_of_thread.items():
            if len(copies) < 2:
                continue
            copies.sort(key=lambda s: s.created_ms)
            keep = max(copies, key=lambda s: (bool(s.native_after_import), s.updated_ms))
            for copy in copies:
                if copy is keep:
                    continue
                native = copy.native_after_import or 0
                allowed = not native and not copy.active and not copy.recent and not copy.archived
                reason = f"One of {len(copies)} Claude imports of Codex thread {tid[:13]}."
                reason += (f" It has {native} native turn(s), so it is protected." if native
                           else f" {keep.short_id} is the copy to keep.")
                out.append(DuplicateCandidate("repeat_import", copy.key, keep.key, "provenance",
                                              bool(native), allowed, reason))

        # Content overlap between sessions with no recorded relationship. Only ever "possible".
        by_print: dict[str, list[Session]] = defaultdict(list)
        for s in sessions.values():
            if s.fingerprint and not s.internal:
                by_print[s.fingerprint].append(s)
        reported = {c.session for c in out}
        for group in by_print.values():
            if len(group) < 2:
                continue
            family = _families(group, sessions)
            for s in group:
                others = [o for o in group if family[o.key] != family[s.key]]
                if not others or s.key in reported:
                    continue
                newest = max(others, key=lambda o: o.updated_ms)
                if s.updated_ms >= newest.updated_ms:
                    continue    # the newest member isn't the one to review
                out.append(DuplicateCandidate(
                    "content_overlap", s.key, newest.key, "possible",
                    bool(s.native_after_import), False,
                    f"Opens with the same requests as {newest.short_id}, but no recorded "
                    "provenance connects them. Review before acting; this is not proof."))
        return out

    def native_work_codex(self, session: Session) -> int:
        """Human turns a Codex thread gained after its imported prefix."""
        try:
            info = self.discovery.rollout_scan(session)
        except (OSError, RuntimeError, ValueError):
            return 1    # Unreadable: assume there is work, so nothing is offered for cleanup.
        return info["human_after_import"] if info["import_end"] is not None else info["human"]


def _families(group: list[Session], sessions: dict) -> dict:
    """Sessions related by a recorded link or provider-recorded fork are not duplicates of
    each other, however alike they look."""
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_conversation: dict[str, tuple] = {}
    for s in group:
        find(s.key)
        if s.conversation_id:
            first = by_conversation.setdefault(s.conversation_id, s.key)
            parent[find(s.key)] = find(first)
        if s.forked_from:
            target = ("codex" if s.provider is CODEX else "claude", s.forked_from)
            ancestor = sessions.get(target)
            parent[find(s.key)] = find(target)
            if ancestor is not None and ancestor.conversation_id:
                first = by_conversation.setdefault(ancestor.conversation_id, target)
                parent[find(target)] = find(first)
    return {s.key: find(s.key) for s in group}


def codex_records_after(raw: dict, path: str, offset: int | None, sha: str | None = None
                        ) -> tuple[list[tuple[int, dict]], str]:
    """Records of a thread past a recorded position, walking its revision chain.

    Returns (records, problem). A revert that discarded transferred history is a
    problem, not an empty delta: the destination holds turns the source disowned.
    """
    chain = raw.get("lineage") or [(raw["path"], None)]
    if offset is None:
        return [], ""
    index = next((i for i, (p, _) in enumerate(chain) if converter.same_rollout(p, path)), None)
    if index is None:
        return [], (f"the thread's current history no longer includes the transferred "
                    f"rollout {Path(converter.uncompressed_name(path)).name}")
    records: list[tuple[int, dict]] = []
    for i, (rollout, cutoff) in enumerate(chain[index:]):
        data = converter.rollout_bytes(rollout)
        if i == 0:
            if offset > len(data):
                return [], "rollout is shorter than the recorded checkpoint"
            if sha:
                import hashlib
                if hashlib.sha256(data[:offset]).hexdigest() != sha:
                    return [], "the rollout's earlier content changed since the checkpoint"
            if cutoff is not None and cutoff < offset:
                return [], "a revert removed turns that were already transferred"
            start = offset
        else:
            start = 0
        if cutoff is not None:
            data = data[:cutoff]
        records.extend(converter.records_from(data, start))
    return records, ""


def _offset_of_sha(path: Path, digest: str | None) -> int | None:
    """Where a recorded whole-file hash now ends in a transcript that only grows."""
    if not digest:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return converter.prefix_offset(data, digest)


def _epoch_iso(seconds) -> str:
    return scan.iso_of_ms(float(seconds) * 1000) if seconds else ""


def _utc_marker(iso: str) -> str:
    """A transcript-comparable ISO stamp (their timestamps end in Z with milliseconds)."""
    ms = scan.ms_of_iso(iso)
    if not ms:
        return iso or ""
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
