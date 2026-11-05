"""Read-only transcript scanners: what work a Claude transcript or Codex rollout holds,
counted from an arbitrary byte position. Nothing here writes to provider files."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import codex_import as ci

# Bump when scan output changes, so cached results are recomputed.
SCANNER_VERSION = 3

_SKIP_TYPES = (b'"type":"api-request', b'"type": "api-request')
_FINGERPRINT_TURNS = 5
SYNTHETIC_MODELS = {ci.IMPORT_MODEL, "<synthetic>"}
_NOT_HUMAN = ("[Request interrupted", "<task-notification", "Caveat: ")


def _normal(text: str) -> str:
    return " ".join(text.split())


def fingerprint_of(prompts: list[str]) -> str:
    """Hash of the opening human turns; evidence of shared content, never identity."""
    if len(prompts) < 3:
        return ""
    joined = "␞".join(_normal(p)[:2000] for p in prompts[:_FINGERPRINT_TURNS])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


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
    if content.lstrip().startswith(_NOT_HUMAN):
        return None
    human = ci.split_user_message(content)[1]
    return human or None


def _assistant_model(rec: dict) -> str | None:
    if rec.get("type") != "assistant" or rec.get("isSidechain"):
        return None
    return (rec.get("message") or {}).get("model") or ""


def iter_claude(path: Path, start: int = 0):
    """(offset past the line, record) from byte `start`, tolerating a half-written tail."""
    with Path(path).open("rb") as fh:
        fh.seek(start)
        pos = start
        for raw in fh:
            pos += len(raw)
            if not raw.endswith(b"\n"):
                return              # still being written; next time
            if any(t in raw[:400] for t in _SKIP_TYPES):
                continue
            try:
                yield pos, json.loads(raw)
            except ValueError:
                continue


def scan_claude(path: Path, start: int = 0, since_ts: str = "") -> dict:
    """Summarise a Claude transcript from `start`. `since_ts` further ignores chain
    records at or before that time, for links with no recorded byte position."""
    info = {
        "session_id": "", "custom_title": "", "first_human": "", "cwd": "", "first_ts": "",
        "last_ts": "", "git_branch": "", "human": 0, "native_assistant": 0,
        "imported_assistant": 0, "provenance": None, "mark_end": None,
        "native_after_mark": 0, "open_tool_use": False, "fingerprint": "",
        "end": start, "entrypoint": "", "model": "", "native_chain": 0, "native_human": 0,
        "first_session_id": "",
    }
    prompts: list[str] = []
    open_tools: set[str] = set()
    for pos, rec in iter_claude(path, start):
        info["end"] = pos
        kind = rec.get("type")
        # A fork keeps its parent's id on the records it copied.
        info["first_session_id"] = info["first_session_id"] or rec.get("sessionId") or ""
        if kind == "custom-title":
            if rec.get("customTitle"):
                info["custom_title"] = rec["customTitle"]
            if rec.get("importedFrom") == ci.IMPORT_MARK:
                info["mark_end"] = pos
                info["native_after_mark"] = 0
                prov = {k: rec.get(k) for k in ("codexThreadId", "codexRollout",
                                                "codexRolloutSha256", "importedAt",
                                                "syncedAt", "importMode")}
                if prov.get("importedAt") is None and info["provenance"]:
                    prov["importedAt"] = info["provenance"].get("importedAt")
                info["provenance"] = prov
            continue
        if kind not in ("user", "assistant") or rec.get("isSidechain"):
            continue
        ts = rec.get("timestamp") or ""
        if since_ts and ts and ts <= since_ts:
            continue
        info["session_id"] = info["session_id"] or rec.get("sessionId") or ""
        info["cwd"] = rec.get("cwd") or info["cwd"]
        info["git_branch"] = rec.get("gitBranch") or info["git_branch"]
        info["entrypoint"] = rec.get("entrypoint") or info["entrypoint"]
        if ts:
            info["first_ts"] = info["first_ts"] or ts
            info["last_ts"] = ts
        model = _assistant_model(rec)
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    open_tools.add(block.get("id", ""))
                elif block.get("type") == "tool_result":
                    open_tools.discard(block.get("tool_use_id", ""))
        if model is not None:
            if model in SYNTHETIC_MODELS:
                info["imported_assistant"] += int(model == ci.IMPORT_MODEL)
            else:
                info["native_assistant"] += 1
                info["native_after_mark"] += 1
                info["model"] = model
                info["native_chain"] += 1
            continue
        said = human_text(rec)
        if said:
            info["human"] += 1
            if len(prompts) < _FINGERPRINT_TURNS:
                prompts.append(said)
            info["first_human"] = info["first_human"] or said
            # A converted turn in a sync is not native work either, but those carry
            # their assistant replies with the import model, so only count real ones.
            if rec.get("entrypoint") != "cli" or not _is_converter_record(rec):
                info["native_after_mark"] += 1
                info["native_chain"] += 1
                info["native_human"] += 1
    info["open_tool_use"] = bool(open_tools)
    info["fingerprint"] = fingerprint_of(prompts)
    return info


def _is_converter_record(rec: dict) -> bool:
    """Records the converter emits have exactly its envelope; live Claude Code records
    always add fields such as `promptId` or `origin`."""
    return not ({"promptId", "origin", "promptSource", "permissionMode"} & rec.keys())


def scan_rollout_bytes(data: bytes, start: int = 0) -> dict:
    """Summarise Codex rollout bytes from `start`."""
    info = {"import_end": None, "human": 0, "human_after_import": 0, "assistant": 0,
            "assistant_after_import": 0, "last_ts": "", "first_ts": "", "open_turn": False,
            "end": start, "fingerprint": "", "first_human": "", "prompts": []}
    prompts: list[str] = info["prompts"]
    open_turns: set[str] = set()
    for off, rec in ci.records_from(data, start):
        info["end"] = off
        kind = rec.get("type")
        payload = rec.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type")
        if kind == "event_msg":
            turn = str(payload.get("turn_id", ""))
            if ptype == "task_started":
                open_turns.add(turn)
            elif ptype in ("task_complete", "turn_aborted"):
                open_turns.discard(turn)
                if ptype == "task_complete" and turn.startswith(ci.EXTERNAL_TURN_PREFIX):
                    info["import_end"] = off
                    info["human_after_import"] = 0
                    info["assistant_after_import"] = 0
            continue
        if kind != "response_item" or ptype not in ci.CONTENT_TYPES:
            continue
        ts = rec.get("timestamp") or ""
        if ts:
            info["first_ts"] = info["first_ts"] or ts
            info["last_ts"] = ts
        if ptype == "message" and payload.get("role") == "user":
            human = ci.split_user_message(ci.text_of(payload.get("content")))[1]
            if human and not human.lstrip().startswith(_NOT_HUMAN):
                info["human"] += 1
                info["human_after_import"] += 1
                info["first_human"] = info["first_human"] or human
                if len(prompts) < _FINGERPRINT_TURNS:
                    prompts.append(human)
        elif ptype == "message" and payload.get("role") == "assistant":
            info["assistant"] += 1
            info["assistant_after_import"] += 1
    info["open_turn"] = bool(open_turns)
    info["fingerprint"] = fingerprint_of(prompts)
    return info


def sha_prefix(path: Path, length: int) -> str | None:
    """sha256 of a file's first `length` bytes, or None if the file is shorter."""
    h = hashlib.sha256()
    left = length
    with Path(path).open("rb") as fh:
        while left > 0:
            chunk = fh.read(min(1 << 20, left))
            if not chunk:
                return None
            h.update(chunk)
            left -= len(chunk)
    return h.hexdigest()


def file_state(path: Path) -> tuple[int, int]:
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


def iso_of_ms(ms: int | float | None) -> str:
    if not ms:
        return ""
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def ms_of_iso(ts: str | None) -> int:
    if not ts:
        return 0
    try:
        return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0
