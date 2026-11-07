"""Convert OpenAI Codex rollout transcripts into Claude Code sessions.

Codex writes one JSONL "rollout" per thread under ~/.codex/sessions/YYYY/MM/DD/.
Claude Code reads one JSONL per session from ~/.claude/projects/<slug>/<uuid>.jsonl,
where <slug> is the session's cwd with every non-alphanumeric character replaced by
a dash. The desktop app additionally needs a session card (see desktop_register.py).

Names written to disk (`codex-imports.json`, `codex-import-backups`, the `codex-import`
and `codex-imported` markers) identify sessions that already exist, so they must not change.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .context_budget import fit_context
from .history_summary import evidence_turns, offline_digest, claude_summary

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
# Codex records every Claude session it imported here.
CODEX_IMPORT_LEDGER = Path.home() / ".codex" / "external_agent_session_imports.json"
# Our own ledger. It lives beside the data it describes, not with the app: moving or
# deleting the app must not orphan the imports.
LEDGER = Path.home() / ".claude" / "codex-imports.json"

TOOL_VERSION = "1.0"

# Stamped into every assistant record we synthesise.
IMPORT_MODEL = "codex-imported"
# Stamped on the trailing custom-title record of every transcript we write. Unlike
# IMPORT_MODEL it is present even in a session with no assistant turns, so it identifies
# our transcripts when the ledger is missing.
IMPORT_MARK = "codex-import"

# Codex record types that carry conversation content. Everything else in a rollout
# (event_msg/*, token_count, token_usage_record, world_state, thread_settings_applied)
# is either UI-stream duplication of these or pure telemetry.
CONTENT_TYPES = {"message", "reasoning", "function_call", "function_call_output",
                 "custom_tool_call", "custom_tool_call_output"}

# Codex tool -> Claude Code tool. Only names whose *input shape* we can honestly
# rewrite are mapped; everything else keeps its Codex name behind a prefix so it is
# visibly foreign rather than silently impersonating a real Claude tool.
TOOL_MAP = {"exec": "Bash", "shell": "Bash", "local_shell": "Bash"}

# When Codex imports a Claude session it replays that history as turns with these ids,
# ending in a task_complete. Everything after the last one was done in Codex.
EXTERNAL_TURN_PREFIX = "external-import-turn"

# Where transcripts are copied before new Codex turns are appended to them.
BACKUPS = Path.home() / ".claude" / "codex-import-backups"


def configure_paths(codex_home: Path | str | None = None,
                    claude_home: Path | str | None = None) -> None:
    """Point every module-level location at alternate Codex/Claude homes.

    ContextBridge resolves its roots once at start-up (tests and sandboxes use this
    to stay away from the real ~/.codex and ~/.claude).
    """
    global CODEX_SESSIONS, CODEX_IMPORT_LEDGER, CLAUDE_PROJECTS, LEDGER, BACKUPS
    if codex_home is not None:
        codex_home = Path(codex_home)
        CODEX_SESSIONS = codex_home / "sessions"
        CODEX_IMPORT_LEDGER = codex_home / "external_agent_session_imports.json"
    if claude_home is not None:
        claude_home = Path(claude_home)
        CLAUDE_PROJECTS = claude_home / "projects"
        LEDGER = claude_home / "codex-imports.json"
        BACKUPS = claude_home / "codex-import-backups"


# ------------------------------------------------------- machine-preamble stripping
#
# Codex packs machine-generated content into `role: "user"` messages: a
# <recommended_plugins> catalogue (often thousands of words), the project's AGENTS.md,
# an <environment_context> block, pasted-file manifests. Imported verbatim, all of it
# renders as though the user typed it — the single biggest source of mess. Anything
# matched here is background, and is emitted as `isMeta: true` instead.

WRAPPER_TAGS = ("recommended_plugins", "system-reminder", "command-name",
                "command-message", "command-args", "command-contents",
                "user_instructions", "environment_context", "turn_aborted",
                "local-command-stdout", "local-command-caveat", "INSTRUCTIONS",
                "user-prompt-submit-hook")
_TAGS = "|".join(WRAPPER_TAGS)
WRAPPER_RE = re.compile(rf"<({_TAGS})\b.*?</\1>|<({_TAGS})\b[^>]*/?>", re.S | re.I)

# Codex separates its preamble from the real prompt with this header. When present
# it is authoritative — everything after it is what the human actually typed.
REQUEST_RE = re.compile(r"^#{1,6}[ \t]*My request:?[ \t]*$", re.M | re.I)
# Codex internal reviewer/app handoffs can be logged as user-role messages.
# Do not present these as something the human typed.
INTERNAL_PROMPT_RE = re.compile(
    r"^\s*(?:<external_codex_apps_[\w-]+>|"
    r"The following is the Codex agent history\b)", re.I)

# "# Files pasted by the user:" … runs until the next top-level heading.
FILES_BLOCK_RE = re.compile(
    r"^#{1,6}[ \t]*Files (?:mentioned|pasted|attached) by the user:?.*?(?=^#{1,6}[ \t]|\Z)",
    re.M | re.S | re.I)
AGENTS_HDR_RE = re.compile(r"^#{1,6}[ \t]*(AGENTS|CLAUDE)\.md instructions[ \t]*$", re.M | re.I)

MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
URL_RE = re.compile(r"https?://([^/\s]+)(/\S*)?")
# Boilerplate that survives stripping but says nothing about the thread.
NOISE_RE = re.compile(
    r"^(files (mentioned|pasted|attached) by the user|pasted text contains|"
    r"background command completed|my request|attachments?:|"
    r"distinguish instructions in attached documents|"
    r"agents\.md instructions|claude\.md instructions)\b", re.I)
# Attachment manifest entries — "some-name: /Users/…/file" — name a file, not a topic.
MANIFEST_RE = re.compile(r"^\S.{0,80}?:\s*[~/]\S+$")


def split_user_message(raw: str) -> tuple[str, str]:
    """Split a Codex user turn into (background, human). Either may be empty."""
    raw = raw or ""
    if INTERNAL_PROMPT_RE.match(raw):
        return raw.strip(), ""
    m = REQUEST_RE.search(raw)
    if m:
        return raw[:m.start()].strip(), raw[m.end():].strip()

    removed: list[str] = []

    def cut(mo: re.Match) -> str:
        removed.append(mo.group(0))
        return "\n"

    txt = WRAPPER_RE.sub(cut, raw)
    txt = FILES_BLOCK_RE.sub(cut, txt)
    txt = AGENTS_HDR_RE.sub(cut, txt)
    human = txt.strip()
    background = "\n\n".join(x.strip() for x in removed).strip()
    if not human:
        return (background or raw.strip()), ""
    return background, human


def clean_title(raw: str) -> str:
    """First line of real prose in a user turn, tidied enough to read in a sidebar."""
    _, human = split_user_message(raw)
    human = MD_LINK_RE.sub(r"\1", human)
    # A pasted URL is rarely what the thread is *about*, and it eats the whole
    # sidebar width. Drop it and keep the prose around it.
    human = URL_RE.sub(" ", human)
    for line in human.splitlines():
        line = line.strip().strip("#").strip().strip('"“”\'')
        if len(line) < 12 or line.startswith(("- ", "* ", "/", "|", "```")):
            continue
        if NOISE_RE.match(line) or MANIFEST_RE.match(line):
            continue
        return re.sub(r"\s+", " ", line)[:60].strip()
    return ""


# --------------------------------------------------------------------------- utils

def slug_for(cwd: str) -> str:
    """Claude Code's project-directory rule: every non-alphanumeric char becomes '-'."""
    return re.sub(r"[^a-zA-Z0-9]", "-", cwd)


# Codex Desktop gives every thread its own throwaway workspace at
# ~/Documents/Codex/<date>/<thread-slug>. Imported as-is, each one becomes its own
# folder in the Claude sidebar — ten threads, ten junk folders named after truncated
# prompt slugs. They collapse to the shared parent instead.
SCRATCH_RE = re.compile(
    r"^(?P<root>.*[/\\]Documents[/\\]Codex)[/\\]\d{4}-\d{2}-\d{2}[/\\][^/\\]+[/\\]?$")


def group_scratch(cwd: str) -> str:
    m = SCRATCH_RE.match(cwd or "")
    return m.group("root") if m else cwd


def normalize_path(raw: str | os.PathLike | None) -> str:
    """Strip Windows extended-length prefixes Codex stores in its ledger and state DB."""
    text = os.fspath(raw) if raw else ""
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[8:]
    if text.startswith("\\\\?\\"):
        return text[4:]
    return text


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write through a sibling temp file: readers see the old or new file, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with temp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def durable_append(path: Path, data: bytes) -> None:
    """Append bytes and flush them to disk; line endings are written as given."""
    with path.open("ab") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def jsonl_bytes(lines: list[str]) -> bytes:
    return ("\n".join(lines) + "\n").encode("utf-8")


def iso(ts: str | None) -> str:
    if ts:
        return ts
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ms_of(ts: str) -> int | None:
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def git_branch(cwd: str) -> str:
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def claude_version() -> str:
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip().split()[0]
    except Exception:
        return "2.1.271"


def text_of(blocks) -> str:
    """Flatten a Codex content/output array into plain text."""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") in ("input_text", "output_text", "text"):
            parts.append(b.get("text", ""))
        elif b.get("type") == "input_image":
            parts.append("[image]")
    return "".join(parts)


def unwrap_exec(raw: str) -> str:
    """Codex `exec` output is a JSON envelope around the real stdout. Dig it out."""
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    if isinstance(obj, dict) and "output" in obj and isinstance(obj["output"], str):
        exit_code = obj.get("exit_code")
        body = obj["output"]
        return body if exit_code in (0, None) else f"(exit {exit_code})\n{body}"
    return raw


# ----------------------------------------------------------------------- discovery

def _zstd():
    """Codex compresses settled rollouts with zstd. Python 3.14+ ships a codec; older
    interpreters need the `zstandard` package (a declared dependency below 3.14)."""
    try:
        from compression import zstd
        return zstd
    except ImportError:
        pass
    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError(
            "This rollout is zstd-compressed. Use Python 3.14+ or "
            "`pip install zstandard` to read it.") from exc

    class _Shim:
        @staticmethod
        def decompress(data: bytes) -> bytes:
            import io
            with zstandard.ZstdDecompressor().stream_reader(
                    io.BytesIO(data), read_across_frames=True) as reader:
                return reader.read()

        @staticmethod
        def compress(data: bytes) -> bytes:
            return zstandard.ZstdCompressor().compress(data)

        @staticmethod
        def open(path, mode="rb"):
            import io
            raw = open(path, "rb")
            reader = zstandard.ZstdDecompressor().stream_reader(
                raw, closefd=True, read_across_frames=True)
            return io.BufferedReader(reader)
    return _Shim


def is_compressed(path: Path) -> bool:
    return Path(path).name.endswith(".zst")


def rollout_bytes(path: Path) -> bytes:
    """The rollout's JSONL content. Byte offsets everywhere refer to these bytes,
    which is also what Codex's history_base cutoffs count."""
    raw = Path(path).read_bytes()
    return _zstd().decompress(raw) if is_compressed(Path(path)) else raw


def open_rollout(path: Path):
    """Binary stream of decoded JSONL, without loading a compressed file whole."""
    path = Path(path)
    return _zstd().open(path, "rb") if is_compressed(path) else path.open("rb")


def uncompressed_name(path: Path | str) -> str:
    """A rollout keeps its identity when Codex later compresses it in place."""
    text = normalize_path(path)
    return text[:-4] if text.endswith(".zst") else text


def same_rollout(a: Path | str, b: Path | str) -> bool:
    return uncompressed_name(a).lower() == uncompressed_name(b).lower()


def locate_rollout(recorded: Path | str) -> Path:
    """Where a recorded rollout lives now: Codex compresses settled rollouts in place
    and `codex archive` moves them, without either changing what they contain."""
    path = Path(normalize_path(recorded))
    bare = path.name[:-4] if path.name.endswith(".zst") else path.name
    candidates = [path.with_name(bare), path.with_name(bare + ".zst")]
    archived = CODEX_SESSIONS.parent / "archived_sessions"
    candidates += [archived / bare, archived / (bare + ".zst")]
    return next((c for c in candidates if c.is_file()), path)


def records_from(data: bytes, start: int = 0) -> list[tuple[int, dict]]:
    """(offset just past the line, record) for each complete JSON line in data[start:].

    A line Codex is still writing fails to parse and is left for the next run, so an
    offset taken from here never lands inside a record.
    """
    out = []
    pos = start
    while pos < len(data):
        nl = data.find(b"\n", pos)
        end = len(data) if nl < 0 else nl + 1
        line = data[pos:end].strip()
        if line:
            try:
                out.append((end, json.loads(line)))
            except Exception:
                pass
        pos = end
    return out


def prefix_offset(data: bytes, digest: str) -> int | None:
    """Length of the line-aligned prefix of `data` whose sha256 is `digest`.

    Codex only ever appends to a rollout, so for ledger entries written before byte
    offsets were recorded, the sha taken at import time recovers how much of the
    file that import covered.
    """
    h = hashlib.sha256()
    pos = 0
    while True:
        if h.hexdigest() == digest:
            return pos
        if pos >= len(data):
            return None
        nl = data.find(b"\n", pos)
        end = len(data) if nl < 0 else nl + 1
        h.update(data[pos:end])
        pos = end


# Codex's thread/revert writes a new immutable rollout with the SAME thread id
# and a new rollout id after an underscore. Its history_base names the retained
# ancestor prefix. Importing every file independently duplicates one conversation.
ROLLOUT_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"([0-9a-f-]{36})(?:_([0-9a-f-]{36}))?\.jsonl(?:\.zst)?$", re.I)

# thread_source values Codex gives to threads no human started (state DB and rollouts).
INTERNAL_THREAD_SOURCES = {"subagent", "internal", "guardian", "guardian_review",
                           "security_scan", "memory_consolidation"}


def rollout_id(path: Path) -> str | None:
    m = ROLLOUT_RE.match(path.name)
    return (m.group(2) or m.group(1)) if m else None


def is_internal_session(meta: dict) -> bool:
    """Detect Codex subagents and internal reviewers from session metadata."""
    for source in (meta.get("source"), meta.get("thread_source")):
        if isinstance(source, str):
            text = source.strip()
            if not text.startswith("{"):
                if text.lower() in INTERNAL_THREAD_SOURCES:
                    return True
                continue
            try:                          # the state DB stores `source` as JSON text
                source = json.loads(text)
            except ValueError:
                continue
        if isinstance(source, dict) and (
            "subagent" in source or "subAgent" in source or "internal" in source
            or source.get("type") in ("subagent", "internal", "guardian")
        ):
            return True
    return False


def summarize(path: Path) -> dict | None:
    """Read header and title while retaining fields needed for rollout lineage.

    Compressed rollouts stop at the first few user turns: decoding hundreds of
    settled files just to count their lines would make listing take minutes.
    """
    meta, n = None, 0
    candidates: list[str] = []
    compressed = is_compressed(path)
    with open_rollout(path) as fh:
        for raw in fh:
            n += 1
            if meta and len(candidates) >= 6:
                if compressed:
                    break
                continue
            line = raw.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") == "session_meta":
                if meta is None or (r.get("payload") or {}).get("id") == meta.get("id"):
                    meta = r.get("payload", {})
                if compressed and is_internal_session(meta):
                    break
            elif (len(candidates) < 6 and r.get("type") == "response_item"
                  and r.get("payload", {}).get("type") == "message"
                  and r["payload"].get("role") == "user"):
                candidates.append(text_of(r["payload"].get("content")))
    if not meta:
        return None
    return {"path": path, "id": meta.get("id") or meta.get("session_id"),
            "rollout_id": rollout_id(path),
            "history_base": meta.get("history_base"),
            "forked_from": meta.get("forked_from_id"),
            "internal": is_internal_session(meta),
            "cwd": meta.get("cwd", ""), "ts": meta.get("timestamp", ""),
            "originator": meta.get("originator", ""),
            "lines": None if compressed else n,
            "first_user": next((t for t in (clean_title(c) for c in candidates) if t), "")}


def resolve_lineage(sess: dict, rollouts: dict[str, dict]) -> None:
    """Find retained ancestor prefixes, oldest first; never silently truncate."""
    chain = [(sess["path"], None)]
    visited = {sess["rollout_id"]}
    current = sess
    while isinstance(current.get("history_base"), dict):
        base = current["history_base"]
        ancestor_id = base.get("thread_id")
        ancestor = rollouts.get(ancestor_id)
        if not ancestor or ancestor_id in visited:
            sess["lineage_error"] = f"missing/cyclic ancestor rollout {ancestor_id}"
            return
        try:
            end = int(base["end_byte_offset"])
        except (KeyError, TypeError, ValueError):
            sess["lineage_error"] = f"missing byte cutoff for ancestor {ancestor_id}"
            return
        if end < 1:
            sess["lineage_error"] = f"invalid ancestor cutoff {end}"
            return
        visited.add(ancestor_id)
        chain.append((ancestor["path"], end))
        current = ancestor
    sess["lineage"] = list(reversed(chain))


def read_lineage(sess: dict) -> list[dict]:
    """Reconstruct content from each immutable ancestor prefix and final rollout."""
    if sess.get("lineage_error"):
        raise ValueError(sess["lineage_error"])
    records = []
    for path, cutoff in sess.get("lineage", [(sess["path"], None)]):
        raw = rollout_bytes(path)
        if cutoff is not None:
            if cutoff > len(raw) or raw[cutoff - 1:cutoff] != b"\n":
                raise ValueError(f"invalid history_base byte cutoff {cutoff} in {path}")
            raw = raw[:cutoff]
        records.extend(r for _, r in records_from(raw))
    return records


# -------------------------------------------------------------------------- ledger

def load_ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_ledger(d: dict) -> None:
    atomic_write_bytes(LEDGER, json.dumps(d, indent=2, sort_keys=True).encode("utf-8"))


def provenance_banner(sess: dict, mode: str) -> str:
    """Shown as background at the top of an imported thread, so its origin is visible
    when the session is opened, not only recorded in metadata."""
    return (f"Imported from OpenAI Codex by ContextBridge {TOOL_VERSION}.\n"
            f"Codex thread {sess['id']}, started {sess['ts'][:19]} "
            f"({sess['originator'] or 'codex'}).\n"
            f"Source rollout: {sess['path']}\n"
            f"Mode: {mode}. Reasoning is not carried over — Codex stores it encrypted, "
            f"and Anthropic thinking blocks require a signature that cannot be minted.")


# ----------------------------------------------------------------------- conversion

class Converter:
    def __init__(self, sess: dict, mode: str, keep_developer: bool, version: str,
                 cwd: str, session_id: str | None = None, parent: str | None = None):
        self.mode = mode
        self.keep_developer = keep_developer
        self.version = version
        # Both set when continuing an existing transcript rather than starting one.
        self.session_id = session_id or str(uuid.uuid4())
        self.cwd = cwd
        self.branch = git_branch(self.cwd)
        self.parent = parent
        self.out: list[dict] = []
        self.pending_calls: dict[str, str] = {}   # codex call_id -> claude toolu id
        self.open_tool_use: str | None = None     # toolu id awaiting a tool_result
        self.stats = {"user": 0, "assistant": 0, "tool_use": 0, "tool_result": 0,
                      "reasoning_kept": 0, "reasoning_dropped": 0, "background": 0,
                      "orphan_results": 0, "synthetic_results": 0}

    # -- envelope ----------------------------------------------------------------
    def envelope(self, kind: str, ts: str) -> dict:
        u = str(uuid.uuid4())
        rec = {
            "parentUuid": self.parent,
            "isSidechain": False,
            "userType": "external",
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "version": self.version,
            "gitBranch": self.branch,
            "entrypoint": "cli",
            "type": kind,
            "uuid": u,
            "timestamp": iso(ts),
        }
        self.parent = u
        return rec

    def emit_user(self, content, ts: str, meta: bool = False):
        rec = self.envelope("user", ts)
        rec["message"] = {"role": "user", "content": content}
        if meta:
            # Claude Code's own marker for machine-generated turns (its
            # <local-command-caveat> records use it). Keeps the content in the
            # transcript without rendering it as something the user said.
            rec["isMeta"] = True
        self.out.append(rec)
        return rec

    def emit_assistant(self, content: list, ts: str, stop: str = "end_turn"):
        rec = self.envelope("assistant", ts)
        rec["message"] = {
            "id": "msg_codeximport_" + uuid.uuid4().hex[:20],
            "type": "message",
            "role": "assistant",
            "model": IMPORT_MODEL,
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                      "service_tier": "standard"},
        }
        self.out.append(rec)
        return rec

    # -- tool-call pairing --------------------------------------------------------
    def close_open_tool_use(self, ts: str):
        """The API requires every tool_use to be answered. Backfill if Codex didn't."""
        if self.open_tool_use:
            self.stats["synthetic_results"] += 1
            self.emit_user([{"type": "tool_result", "tool_use_id": self.open_tool_use,
                             "content": "(no output recorded in the Codex rollout)",
                             "is_error": False}], ts)
            self.open_tool_use = None

    def do_tool_call(self, p: dict, ts: str):
        call_id = p.get("call_id") or p.get("id") or uuid.uuid4().hex
        raw_name = p.get("name", "tool")
        ns = p.get("namespace")
        full = f"{ns}__{raw_name}" if ns else raw_name

        if p.get("type") == "function_call":
            try:
                args = json.loads(p.get("arguments") or "{}")
            except Exception:
                args = {"arguments": p.get("arguments", "")}
        else:
            args = {"command": p.get("input", ""), "description": f"codex {raw_name}"}

        if self.mode == "archive":
            body = args.get("command") or json.dumps(args, indent=2, ensure_ascii=False)
            self.emit_assistant(
                [{"type": "text", "text": f"**[codex tool call: `{full}`]**\n\n```\n{body}\n```"}], ts)
            self.pending_calls[call_id] = None
            return

        self.close_open_tool_use(ts)
        name = TOOL_MAP.get(raw_name, full if str(full).startswith("mcp__") else f"codex__{full}")
        if name == "Bash" and "description" not in args:
            args["description"] = "imported from Codex"
        tid = "toolu_codeximport_" + uuid.uuid4().hex[:20]
        self.pending_calls[call_id] = tid
        self.open_tool_use = tid
        self.stats["tool_use"] += 1
        self.emit_assistant([{"type": "tool_use", "id": tid, "name": name, "input": args}],
                            ts, stop="tool_use")

    def do_tool_output(self, p: dict, ts: str):
        call_id = p.get("call_id")
        body = unwrap_exec(text_of(p.get("output")))

        if self.mode == "archive":
            self.emit_user(f"**[codex tool output]**\n\n```\n{body}\n```", ts, meta=True)
            return

        tid = self.pending_calls.get(call_id)
        if not tid:
            # A result with no call in this file (e.g. a rollout that starts mid-turn).
            self.stats["orphan_results"] += 1
            self.emit_user(f"**[orphaned codex tool output]**\n\n```\n{body}\n```", ts, meta=True)
            return
        self.stats["tool_result"] += 1
        self.open_tool_use = None
        rec = self.emit_user([{"type": "tool_result", "tool_use_id": tid,
                               "content": body, "is_error": False}], ts)
        rec["toolUseResult"] = {"stdout": body, "stderr": "", "interrupted": False,
                                "isImage": False}

    # -- main loop ----------------------------------------------------------------
    def run(self, recs: list[dict]) -> list[dict]:
        for r in recs:
            if r.get("type") != "response_item":
                continue
            p = r.get("payload") or {}
            t = p.get("type")
            if t not in CONTENT_TYPES:
                continue
            ts = r.get("timestamp", "")

            if t == "message":
                role = p.get("role")
                body = text_of(p.get("content")).strip()
                if not body:
                    continue
                if role == "user":
                    self.close_open_tool_use(ts)
                    background, human = split_user_message(body)
                    if background:
                        self.stats["background"] += 1
                        self.emit_user(background, ts, meta=True)
                    if human:
                        self.stats["user"] += 1
                        self.emit_user(human, ts)
                elif role == "assistant":
                    self.stats["assistant"] += 1
                    self.emit_assistant([{"type": "text", "text": body}], ts)
                elif role == "developer":
                    if self.keep_developer:
                        self.stats["background"] += 1
                        self.emit_user(body, ts, meta=True)

            elif t == "reasoning":
                # Codex reasoning lives in `encrypted_content`, an opaque OpenAI blob.
                # It cannot become a Claude `thinking` block: those carry an
                # Anthropic-issued `signature` that is verified on resume, and there
                # is no way to mint one. Only the plaintext summary survives.
                summary = " ".join(s.get("text", "") for s in (p.get("summary") or [])
                                   if isinstance(s, dict)).strip()
                if summary:
                    self.stats["reasoning_kept"] += 1
                    self.emit_assistant([{"type": "text", "text": f"_(codex reasoning)_ {summary}"}], ts)
                else:
                    self.stats["reasoning_dropped"] += 1

            elif t in ("function_call", "custom_tool_call"):
                self.do_tool_call(p, ts)

            elif t in ("function_call_output", "custom_tool_call_output"):
                self.do_tool_output(p, ts)

        self.close_open_tool_use(self.out[-1]["timestamp"] if self.out else "")
        return self.out


def validate(recs: list[dict], root: str | None = None) -> list[str]:
    """Check the invariants the Messages API enforces on replayed history.

    `root` is the uuid the first record hangs off when `recs` continues a transcript.
    """
    errs = []
    seen = set()
    prev = root
    open_ids: set[str] = set()
    for i, r in enumerate(recs):
        u = r.get("uuid")
        if u in seen:
            errs.append(f"line {i}: duplicate uuid {u}")
        seen.add(u)
        if r.get("parentUuid") != prev:
            errs.append(f"line {i}: parentUuid chain broken")
        prev = u
        content = (r.get("message") or {}).get("content")
        if isinstance(content, list):
            for b in content:
                if b.get("type") == "tool_use":
                    open_ids.add(b["id"])
                elif b.get("type") == "tool_result":
                    tid = b.get("tool_use_id")
                    if tid not in open_ids:
                        errs.append(f"line {i}: tool_result for unknown tool_use {tid}")
                    else:
                        open_ids.discard(tid)
    for tid in open_ids:
        errs.append(f"unanswered tool_use {tid}")
    return errs


# ----------------------------------------------------------------------------- sync
#
# A thread can move on in Codex after it reached Claude: an import of ours the user
# kept working on in Codex, or a Claude session Codex imported (a "round-trip") that
# was continued there. Either way the new Codex turns are appended to the Claude
# transcript that already holds the thread, so it stays one conversation.

# Records that sit on the conversation chain. Others (last-prompt, custom-title,
# file-history-snapshot, ...) are session metadata and must not be a parent.
CHAIN_TYPES = {"user", "assistant", "system", "attachment"}


SYNTHETIC_MODELS = {IMPORT_MODEL, "<synthetic>"}
NOT_HUMAN_PREFIXES = ("[Request interrupted", "<task-notification", "Caveat: ")
# Live Claude Code records carry at least one of these; the converter's envelope has none.
LIVE_RECORD_FIELDS = {"promptId", "origin", "promptSource", "permissionMode"}


def is_converter_record(rec: dict) -> bool:
    return rec.get("entrypoint") == "cli" and not (LIVE_RECORD_FIELDS & rec.keys())


def human_text(rec: dict) -> str | None:
    """The text a person typed in a Claude `user` record, or None for anything else."""
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    origin = rec.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return None
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        content = "\n".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(content, str) or not content.strip():
        return None
    if content.lstrip().startswith(NOT_HUMAN_PREFIXES):
        return None
    return split_user_message(content)[1] or None


def transcript_tail(path: Path) -> dict:
    """Where an existing Claude transcript's conversation ends, and what it is called."""
    info = {"leaf": None, "cwd": None, "sessionId": None, "title": None, "lastTs": ""}
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") in CHAIN_TYPES and r.get("uuid") and not r.get("isSidechain"):
                info["leaf"] = r["uuid"]
                info["cwd"] = r.get("cwd") or info["cwd"]
                info["sessionId"] = r.get("sessionId") or info["sessionId"]
                info["lastTs"] = r.get("timestamp") or info["lastTs"]
            elif r.get("type") == "custom-title":
                info["title"] = r.get("customTitle") or info["title"]
    return info


def sync_banner(sess: dict, mode: str, first: str, last: str) -> str:
    return (f"Continued in OpenAI Codex, {first[:16].replace('T', ' ')} to "
            f"{last[:16].replace('T', ' ')} UTC. Brought across by "
            f"ContextBridge {TOOL_VERSION} from Codex thread {sess['id']}.\n"
            f"Source rollout: {sess['path']}\n"
            f"Mode: {mode}. Reasoning is not carried over.")


def append_codex_records(sess: dict, recs: list[tuple[int, dict]], start: int, target: Path,
                         *, mode: str, keep_developer: bool, version: str,
                         banner: bool = True, native: bool = False, dry_run: bool = False,
                         digest_of=None, transform=None) -> dict:
    """Convert `recs` and continue `target`'s conversation with them.

    `native` means the transcript is a Claude session rather than one this app wrote, so
    no import title or provenance record is appended to it.

    `recs` are (offset past the line, record) pairs not yet in `target`; `digest_of`
    fingerprints the source up to an offset. `transform`, when given, may rewrite the
    converted records (bounded handoffs shorten large tool output) before validation.
    """
    result = {"end": recs[-1][0] if recs else start, "out": [], "errs": [],
              "backup": None, "tail": None}
    content = [r for _, r in recs if r.get("type") == "response_item"
               and (r.get("payload") or {}).get("type") in CONTENT_TYPES]
    if not content:
        return result

    tail = transcript_tail(target)
    result["tail"] = tail
    conv = Converter(sess, mode, keep_developer, version,
                     tail["cwd"] or sess["cwd"] or str(Path.home()),
                     session_id=tail["sessionId"] or target.stem, parent=tail["leaf"])
    first, last = content[0].get("timestamp", ""), content[-1].get("timestamp", "")
    if banner:
        conv.emit_user(sync_banner(sess, mode, first, last), first, meta=True)
    n0 = len(conv.out)
    out = conv.run([r for _, r in recs])
    if len(out) == n0:
        return result
    if transform:
        out = transform(out)
    result.update(out=out, errs=validate(out, root=tail["leaf"]), stats=conv.stats,
                  first=first, last=last, sessionId=conv.session_id)

    lines = [json.dumps(r, ensure_ascii=False) for r in out]
    said = [r["message"]["content"] for r in out if r["type"] == "user"
            and not r.get("isMeta") and isinstance(r["message"]["content"], str)]
    # Claude Code records the conversation's leaf here; point it past the new turns.
    lines.append(json.dumps({"type": "last-prompt", "lastPrompt": (said or [""])[-1][:200],
                             "leafUuid": out[-1]["uuid"], "sessionId": conv.session_id},
                            ensure_ascii=False))
    digest = digest_of(result["end"]) if digest_of else None
    result["digest"] = digest
    if not native:
        lines.append(json.dumps({
            "type": "custom-title", "customTitle": tail["title"] or sess["first_user"],
            "sessionId": conv.session_id, "importedFrom": IMPORT_MARK,
            "codexThreadId": sess["id"], "codexRollout": str(sess["path"]),
            "codexRolloutSha256": digest,
            "syncedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "importToolVersion": TOOL_VERSION,
        }, ensure_ascii=False))

    if not dry_run and not result["errs"]:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        backup = BACKUPS / f"{target.stem}.{stamp}.jsonl"
        existing = target.read_bytes()
        atomic_write_bytes(backup, existing)
        result["backup"] = backup
        prefix = b"\n" if existing and not existing.endswith(b"\n") else b""
        durable_append(target, prefix + jsonl_bytes(lines))
    return result


# ------------------------------------------------------------------- whole imports


class ImportSkipped(Exception):
    """A session cannot be converted; the message says why, nothing was written."""


class ReplaceRefused(ValueError):
    """A bounded session cannot be rebuilt in place; the message says why."""


def build_import(s: dict, args, version: str, replace_entry: dict | None = None,
                 extra_banner: str | None = None) -> dict:
    """Convert one Codex thread to Claude records, in memory, writing nothing.

    `args` needs the import option attributes.
    A dry-run swaps Claude summaries for the offline digest, so previews stay free.
    """
    if s.get("lineage_error"):
        raise ImportSkipped(f"cannot reconstruct history: {s['lineage_error']}")
    try:
        recs = read_lineage(s)
        # Sync offsets refer to the active rollout only, not concatenated ancestry.
        data = rollout_bytes(s["path"])
    except (OSError, ValueError, RuntimeError) as err:
        raise ImportSkipped(f"cannot reconstruct history: {err}") from err
    parsed = records_from(data)
    consumed = parsed[-1][0] if parsed else 0
    cwd = normalize_path(s["cwd"]) or str(Path.home())
    if not args.no_group_scratch:
        cwd = group_scratch(cwd)
    conv = Converter(s, args.mode, args.keep_developer, version, cwd)
    if not args.no_banner:
        conv.emit_user(provenance_banner(s, args.mode), s["ts"], meta=True)
    if extra_banner:
        conv.emit_user(extra_banner, s["ts"], meta=True)
    out = conv.run(recs)
    if not has_conversation(conv.stats):
        raise ImportSkipped("no human conversation content")
    budget_info = None
    summary_info = None
    if args.context_budget is not None:
        try:
            original_out = out
            with_summary = (args.summary_mode != "off" or bool(args.summary_file))
            reserve = args.summary_tokens if with_summary else 0
            preview, _ = fit_context(original_out, args.context_budget,
                                     max_tool_chars=args.max_tool_chars,
                                     summary_reserve_tokens=reserve)
            keep_uuids = {r["uuid"] for r in preview if r.get("uuid")}
            earlier = [r for r in original_out if r.get("uuid") not in keep_uuids]
            turns = evidence_turns(earlier)
            summary = None
            summary_info = {"mode": "off", "turns": len(turns)}
            if earlier and with_summary:
                if args.summary_file:
                    summary = Path(args.summary_file).read_text(encoding="utf-8")
                    summary_info["mode"] = "file"
                elif args.summary_mode == "claude" and not args.dry_run:
                    summary, ai_stats = claude_summary(
                        turns, model=args.summary_model,
                        max_tokens=args.summary_tokens,
                        max_usd_per_call=args.summary_max_usd,
                        max_calls=args.summary_max_calls,
                        thread_id=s["id"])
                    summary_info.update(mode="claude", **ai_stats)
                else:
                    summary = offline_digest(turns, max_tokens=args.summary_tokens)
                    summary_info["mode"] = "index"
                    if args.summary_mode == "claude":
                        summary_info["dry_run_skipped_claude"] = True
                if not summary or not summary.strip():
                    raise ValueError("history summary was empty")
            out, budget_info = fit_context(
                original_out, args.context_budget, max_tool_chars=args.max_tool_chars,
                historical_summary=summary, summary_reserve_tokens=reserve)
        except (ValueError, RuntimeError, OSError) as err:
            raise ImportSkipped(f"context summary failed: {err}") from err

    if replace_entry:
        for rec in out:
            rec["sessionId"] = replace_entry["claudeSessionId"]
        conv.session_id = replace_entry["claudeSessionId"]
    errs = validate(out)
    if errs:
        raise ImportSkipped(f"transcript validation failed: {errs[:3]}")

    base = s["first_user"] or "Codex import"
    title = f"{args.title_prefix}{base}" if args.title_prefix else base
    lines = [json.dumps(r, ensure_ascii=False) for r in out]
    digest = hashlib.sha256(data[:consumed]).hexdigest()
    lines.append(json.dumps({
        "type": "custom-title", "customTitle": title,
        "sessionId": conv.session_id,
        # Provenance, carried in the transcript itself so it survives losing the
        # ledger, moving the file, or deleting this checkout.
        "importedFrom": IMPORT_MARK,
        "codexThreadId": s["id"],
        "codexRollout": str(s["path"]),
        "codexRolloutSha256": digest,
        "importedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "importMode": args.mode,
        "importToolVersion": TOOL_VERSION,
    }, ensure_ascii=False))
    return {"conv": conv, "out": out, "cwd": cwd, "title": title, "lines": lines,
            "digest": digest, "consumed": consumed, "budget": budget_info,
            "summary": summary_info, "errs": errs}


def write_import(target: Path, lines: list[str], *, replace: bool = False) -> Path | None:
    """Atomically create or (after a backup) replace a transcript. Returns the backup."""
    backup = None
    if replace:
        BACKUPS.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        backup = BACKUPS / f"{target.stem}.{stamp}.jsonl"
        atomic_write_bytes(backup, target.read_bytes())
    atomic_write_bytes(target, jsonl_bytes(lines))
    return backup


def ledger_entry(s: dict, built: dict, target: Path, card, args) -> dict:
    return {
        "claudeSessionId": built["conv"].session_id,
        "transcript": str(target),
        "card": str(card) if card else None,
        "cwd": built["cwd"],
        "title": built["title"],
        "importedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sourceRollout": str(s["path"]),
        "sourceSha256": built["digest"],
        "sourceMtime": int(s["path"].stat().st_mtime),
        "sourceBytes": built["consumed"],
        "kind": "import",
        "mode": args.mode,
        "contextBudget": args.context_budget,
        "summaryMode": built["summary"]["mode"] if built["summary"] else "off",
        "summaryTokens": args.summary_tokens if built["summary"] else None,
        "toolVersion": TOOL_VERSION,
    }


def verify_replace_target(target: Path, entry: dict, claude_dir: str) -> None:
    """Never replace a native or continued Claude session by accident."""
    if not target.is_file():
        raise ReplaceRefused(f"The imported transcript is missing: {target}")
    if not target.resolve().is_relative_to(Path(claude_dir).resolve()):
        raise ReplaceRefused(f"The target is outside Claude projects: {target}")
    if target.stem != entry.get("claudeSessionId"):
        raise ReplaceRefused("The existing transcript has an unexpected session ID.")
    found_mark = False
    with target.open(encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("type") == "custom-title" and record.get("importedFrom") == IMPORT_MARK:
                found_mark = True
            if record.get("type") == "assistant":
                model = (record.get("message") or {}).get("model")
                if model != IMPORT_MODEL:
                    raise ReplaceRefused("Claude has continued this session; use a new session "
                                         "instead to preserve those turns.")
    if not found_mark:
        raise ReplaceRefused("The transcript has no import provenance marker.")


def has_conversation(stats: dict) -> bool:
    """Do not import an internal-only background transcript."""
    return any(stats[k] for k in ("user", "assistant", "tool_use", "tool_result"))
