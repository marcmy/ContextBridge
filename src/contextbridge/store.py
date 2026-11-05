"""The durable local lineage database.

SQLite in WAL mode; one connection per thread. This is ContextBridge's own record:
native ledgers are read and reconciled into it, never replaced by it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import Link, Provider

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    origin_provider TEXT,
    origin_session TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    path TEXT NOT NULL DEFAULT '',
    cwd TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    size INTEGER,
    fingerprint TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (provider, session_id)
);
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    src_provider TEXT NOT NULL,
    src_id TEXT NOT NULL,
    dst_provider TEXT NOT NULL,
    dst_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    origin TEXT NOT NULL,
    transferred_at TEXT NOT NULL,
    src_path TEXT NOT NULL DEFAULT '',
    src_offset INTEGER,
    src_sha TEXT,
    dst_path TEXT NOT NULL DEFAULT '',
    dst_offset INTEGER,
    dst_sha TEXT,
    delta_path TEXT NOT NULL DEFAULT '',
    bounded INTEGER NOT NULL DEFAULT 0,
    meta TEXT NOT NULL DEFAULT '{}',
    UNIQUE (src_provider, src_id, dst_provider, dst_id)
);
CREATE INDEX IF NOT EXISTS links_src ON links (src_provider, src_id);
CREATE INDEX IF NOT EXISTS links_dst ON links (dst_provider, dst_id);
CREATE TABLE IF NOT EXISTS transfers (
    id TEXT PRIMARY KEY,
    link_id INTEGER,
    status TEXT NOT NULL,
    strategy TEXT NOT NULL,
    src_provider TEXT NOT NULL,
    src_id TEXT NOT NULL,
    dst_provider TEXT NOT NULL,
    dst_id TEXT,
    dst_path TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    backup_path TEXT,
    report_path TEXT,
    error TEXT,
    intent TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS archived (
    provider TEXT NOT NULL,
    session_id TEXT NOT NULL,
    original_path TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    method TEXT NOT NULL,
    archived_at TEXT NOT NULL,
    restored_at TEXT,
    PRIMARY KEY (provider, session_id, archived_at)
);
CREATE TABLE IF NOT EXISTS scan_cache (
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (path, kind)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._opened: list[sqlite3.Connection] = []
        self._guard = threading.Lock()
        with self.conn as c:
            c.executescript(_SCHEMA)
            if not c.execute("SELECT 1 FROM schema_version").fetchone():
                c.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            # Each thread uses only its own connection; check_same_thread is off just so
            # close() can shut down those opened by finished worker threads.
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            self._local.conn = c
            with self._guard:
                self._opened.append(c)
        return c

    def close(self) -> None:
        with self._guard:
            opened, self._opened = self._opened, []
        for c in opened:
            c.close()
        self._local = threading.local()

    # -- links -------------------------------------------------------------------------
    @staticmethod
    def _link(row: sqlite3.Row) -> Link:
        return Link(
            id=row["id"], src_provider=Provider(row["src_provider"]), src_id=row["src_id"],
            dst_provider=Provider(row["dst_provider"]), dst_id=row["dst_id"],
            relation=row["relation"], origin=row["origin"],
            transferred_at=row["transferred_at"], src_path=row["src_path"],
            src_offset=row["src_offset"], src_sha=row["src_sha"], dst_path=row["dst_path"],
            dst_offset=row["dst_offset"], dst_sha=row["dst_sha"],
            delta_path=row["delta_path"], bounded=bool(row["bounded"]),
            meta=json.loads(row["meta"] or "{}"))

    def links(self) -> list[Link]:
        return [self._link(r) for r in
                self.conn.execute("SELECT * FROM links ORDER BY transferred_at, id")]

    def links_for(self, provider: Provider, sid: str) -> list[Link]:
        rows = self.conn.execute(
            "SELECT * FROM links WHERE (src_provider=? AND src_id=?) "
            "OR (dst_provider=? AND dst_id=?) ORDER BY transferred_at DESC, id DESC",
            (provider.value, sid, provider.value, sid))
        return [self._link(r) for r in rows]

    def find_link(self, a: tuple[Provider, str], b: tuple[Provider, str]) -> Link | None:
        """A pair is one link regardless of which way it was first sent."""
        row = self.conn.execute(
            "SELECT * FROM links WHERE (src_provider=? AND src_id=? AND dst_provider=? AND dst_id=?)"
            " OR (src_provider=? AND src_id=? AND dst_provider=? AND dst_id=?)",
            (a[0].value, a[1], b[0].value, b[1], b[0].value, b[1], a[0].value, a[1])).fetchone()
        return self._link(row) if row else None

    def save_link(self, link: Link) -> Link:
        values = (link.relation, link.origin, link.transferred_at or now_iso(),
                  link.src_path, link.src_offset, link.src_sha, link.dst_path,
                  link.dst_offset, link.dst_sha, link.delta_path, int(link.bounded),
                  json.dumps(link.meta, ensure_ascii=False))
        with self.conn as c:
            if link.id is None:
                cur = c.execute(
                    "INSERT INTO links (relation, origin, transferred_at, src_path, src_offset,"
                    " src_sha, dst_path, dst_offset, dst_sha, delta_path, bounded, meta,"
                    " src_provider, src_id, dst_provider, dst_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values + (link.src_provider.value, link.src_id,
                              link.dst_provider.value, link.dst_id))
                link.id = cur.lastrowid
            else:
                c.execute(
                    "UPDATE links SET relation=?, origin=?, transferred_at=?, src_path=?,"
                    " src_offset=?, src_sha=?, dst_path=?, dst_offset=?, dst_sha=?,"
                    " delta_path=?, bounded=?, meta=? WHERE id=?", values + (link.id,))
        return link

    # -- sessions and conversations ------------------------------------------------------
    def conversation_of(self, provider: str, sid: str) -> str | None:
        row = self.conn.execute(
            "SELECT conversation_id FROM sessions WHERE provider=? AND session_id=?",
            (provider, sid)).fetchone()
        return row["conversation_id"] if row else None

    def upsert_sessions(self, rows: list[tuple]) -> None:
        """(provider, id, conversation, path, cwd, title, size, fingerprint), one transaction."""
        now = now_iso()
        with self.conn as c:
            c.executemany(
                "INSERT INTO sessions (provider, session_id, conversation_id, path, cwd, title,"
                " size, fingerprint, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT (provider, session_id) DO UPDATE SET conversation_id=excluded.conversation_id,"
                " path=excluded.path, cwd=excluded.cwd, title=excluded.title,"
                " last_seen=excluded.last_seen, size=excluded.size,"
                " fingerprint=excluded.fingerprint",
                [(*row, now, now) for row in rows])

    def ensure_conversation(self, conversation_id: str | None, *, title: str,
                            origin_provider: str, origin_session: str) -> str:
        cid = conversation_id or str(uuid.uuid4())
        with self.conn as c:
            c.execute(
                "INSERT OR IGNORE INTO conversations (id, title, origin_provider, origin_session,"
                " created_at) VALUES (?,?,?,?,?)",
                (cid, title, origin_provider, origin_session, now_iso()))
        return cid

    # -- transfers (write-ahead) -----------------------------------------------------------
    def begin_transfer(self, transfer_id: str, *, strategy: str, src: tuple[str, str],
                       dst: tuple[str, str | None], dst_path: str, intent: dict) -> None:
        with self.conn as c:
            c.execute(
                "INSERT INTO transfers (id, status, strategy, src_provider, src_id, dst_provider,"
                " dst_id, dst_path, started_at, intent) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (transfer_id, "writing", strategy, src[0], src[1], dst[0], dst[1], dst_path,
                 now_iso(), json.dumps(intent, ensure_ascii=False)))

    def update_transfer(self, transfer_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.conn as c:
            c.execute(f"UPDATE transfers SET {cols} WHERE id=?", (*fields.values(), transfer_id))

    def transfers(self, limit: int = 500) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM transfers ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def transfer(self, transfer_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM transfers WHERE id=?", (transfer_id,)).fetchone()
        return dict(row) if row else None

    def interrupted_transfers(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM transfers WHERE status='writing'")
        return [dict(r) for r in rows]

    # -- archive ------------------------------------------------------------------------
    def record_archive(self, provider: str, sid: str, original: str, archive: str,
                       method: str) -> None:
        with self.conn as c:
            c.execute("INSERT INTO archived (provider, session_id, original_path, archive_path,"
                      " method, archived_at) VALUES (?,?,?,?,?,?)",
                      (provider, sid, original, archive, method, now_iso()))

    def active_archive(self, provider: str, sid: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM archived WHERE provider=? AND session_id=? AND restored_at IS NULL"
            " ORDER BY archived_at DESC", (provider, sid)).fetchone()
        return dict(row) if row else None

    def archives(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM archived WHERE restored_at IS NULL ORDER BY archived_at DESC")
        return [dict(r) for r in rows]

    def mark_restored(self, provider: str, sid: str) -> None:
        with self.conn as c:
            c.execute("UPDATE archived SET restored_at=? WHERE provider=? AND session_id=?"
                      " AND restored_at IS NULL", (now_iso(), provider, sid))

    # -- scan cache ---------------------------------------------------------------------
    def cached_scan(self, path: str, kind: str, size: int, mtime_ns: int) -> dict | None:
        row = self.conn.execute(
            "SELECT data FROM scan_cache WHERE path=? AND kind=? AND size=? AND mtime_ns=?",
            (path, kind, size, mtime_ns)).fetchone()
        return json.loads(row["data"]) if row else None

    def put_scan(self, path: str, kind: str, size: int, mtime_ns: int, data: dict) -> None:
        with self.conn as c:
            c.execute("INSERT OR REPLACE INTO scan_cache VALUES (?,?,?,?,?)",
                      (path, kind, size, mtime_ns, json.dumps(data, ensure_ascii=False)))
