"""Plain data shared by the backend and the UI."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Provider(str, Enum):
    CODEX = "codex"
    CLAUDE = "claude"

    @property
    def label(self) -> str:
        return {"codex": "Codex", "claude": "Claude"}[self.value]

    @property
    def other(self) -> "Provider":
        return Provider.CLAUDE if self is Provider.CODEX else Provider.CODEX


class SyncState(str, Enum):
    NOT_TRANSFERRED = "not_transferred"
    UNCHANGED = "unchanged"
    SOURCE_ADVANCED = "source_advanced"
    DEST_ADVANCED = "destination_advanced"
    BOTH_ADVANCED = "both_advanced"
    MISSING_DEST = "missing_destination"
    MISSING_SOURCE = "missing_source"
    MODIFIED_SOURCE = "modified_source"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    AMBIGUOUS = "ambiguous"

    @property
    def label(self) -> str:
        return STATE_LABELS[self]

    @property
    def severity(self) -> str:
        """info | ok | action | warn | danger — the UI's colour language."""
        return STATE_SEVERITY[self]


STATE_LABELS = {
    SyncState.NOT_TRANSFERRED: "Not transferred",
    SyncState.UNCHANGED: "Imported, unchanged",
    SyncState.SOURCE_ADVANCED: "Source advanced",
    SyncState.DEST_ADVANCED: "Destination advanced",
    SyncState.BOTH_ADVANCED: "Both advanced",
    SyncState.MISSING_DEST: "Destination missing",
    SyncState.MISSING_SOURCE: "Source missing",
    SyncState.MODIFIED_SOURCE: "Source modified",
    SyncState.POSSIBLE_DUPLICATE: "Possible duplicate",
    SyncState.AMBIGUOUS: "Ambiguous lineage",
}
STATE_SEVERITY = {
    SyncState.NOT_TRANSFERRED: "info",
    SyncState.UNCHANGED: "ok",
    SyncState.SOURCE_ADVANCED: "action",
    SyncState.DEST_ADVANCED: "info",
    SyncState.BOTH_ADVANCED: "danger",
    SyncState.MISSING_DEST: "warn",
    SyncState.MISSING_SOURCE: "warn",
    SyncState.MODIFIED_SOURCE: "warn",
    SyncState.POSSIBLE_DUPLICATE: "warn",
    SyncState.AMBIGUOUS: "danger",
}


@dataclass
class Session:
    """One provider-specific conversation. Identity is (provider, session_id) only."""
    provider: Provider
    session_id: str
    title: str = ""
    cwd: str = ""
    project: str = ""           # repository root when one is found, else cwd
    path: Path | None = None
    created_ms: int = 0
    updated_ms: int = 0
    archived: bool = False
    internal: bool = False
    git_branch: str = ""
    size_bytes: int = 0
    # Provider details the session browser shows.
    source: str = ""
    model: str = ""
    tokens_used: int = 0
    revisions: int = 1
    lineage_error: str = ""
    active: bool = False        # evidence of a live process or a turn in flight
    active_reason: str = ""
    recent: bool = False        # written moments ago; a hint only, e.g. after a usage limit
    # Filled from a transcript scan (may be empty until scanned).
    human_turns: int | None = None
    native_after_import: int | None = None
    fingerprint: str = ""
    forked_from: str = ""       # a provider-recorded fork parent, which is not a duplicate
    # Provenance.
    imported: bool = False
    origin_provider: Provider | None = None
    # Sync view relative to the other provider; see lineage.Reconciler.
    state: SyncState = SyncState.NOT_TRANSFERRED
    state_detail: str = ""
    conversation_id: str = ""
    warnings: list[str] = field(default_factory=list)
    duplicate_note: str = ""
    # Codex-only: the session dict the converter understands.
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.provider.value, self.session_id)

    @property
    def short_id(self) -> str:
        return self.session_id[:13] if self.provider is Provider.CODEX else self.session_id[:8]

    @property
    def native(self) -> bool:
        return not self.imported


@dataclass
class Link:
    """A sync point between two sessions of one logical conversation.

    The pair is symmetric once created: `src` is merely who sent first. Each side holds
    the byte position in its own file up to which the pair is known to agree, so either
    side's later work is "whatever came after my position".
    """
    id: int | None
    src_provider: Provider
    src_id: str
    dst_provider: Provider
    dst_id: str
    relation: str = "import"
    origin: str = "contextbridge"
    transferred_at: str = ""
    src_path: str = ""
    src_offset: int | None = None
    src_sha: str | None = None
    dst_path: str = ""
    dst_offset: int | None = None
    dst_sha: str | None = None
    delta_path: str = ""
    bounded: bool = False
    meta: dict = field(default_factory=dict)

    def side(self, provider: Provider, sid: str) -> str:
        if self.src_provider is provider and self.src_id == sid:
            return "src"
        if self.dst_provider is provider and self.dst_id == sid:
            return "dst"
        raise KeyError((provider, sid))

    def peer_of(self, provider: Provider, sid: str) -> tuple[Provider, str]:
        if self.side(provider, sid) == "src":
            return self.dst_provider, self.dst_id
        return self.src_provider, self.src_id

    def position(self, side: str) -> tuple[str, int | None, str | None]:
        if side == "src":
            return self.src_path, self.src_offset, self.src_sha
        return self.dst_path, self.dst_offset, self.dst_sha

    def set_position(self, side: str, path: str, offset: int | None, sha: str | None) -> None:
        if side == "src":
            self.src_path, self.src_offset, self.src_sha = path, offset, sha
        else:
            self.dst_path, self.dst_offset, self.dst_sha = path, offset, sha


@dataclass
class Delta:
    """Work on one side past its link position."""
    known: bool = True
    missing: bool = False
    modified: bool = False
    human: int = 0
    assistant: int = 0
    last_ts: str = ""
    reason: str = ""

    @property
    def advanced(self) -> bool:
        return self.human > 0 or self.assistant > 0


@dataclass
class Assessment:
    """Both sides of a link, viewed from the session being asked about."""
    link: Link | None
    state: SyncState
    detail: str
    local: Delta = field(default_factory=Delta)
    peer: Delta = field(default_factory=Delta)
    peer_key: tuple[str, str] | None = None


@dataclass
class DuplicateCandidate:
    kind: str                     # circular_copy | repeat_import | content_overlap
    session: tuple[str, str]
    canonical: tuple[str, str] | None
    certainty: str                # provenance | possible
    has_native_work: bool
    cleanup_allowed: bool
    reason: str
