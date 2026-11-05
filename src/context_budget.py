"""Lossy, context-bounded Claude transcript view of a complete Codex history.

The source Codex JSONL files are never changed. This produces a working-session
view, not a substitute for an archival import or an AI-generated project summary.
The token count is a deliberately conservative approximation, not a tokenizer.
"""
from __future__ import annotations

import copy
import json
import uuid


def estimate_tokens(records: list[dict]) -> int:
    """Conservative rough size of the message payload sent to the model.

    UTF-8 byte count / 2 tends to overestimate ordinary English/code token use.
    Include per-message overhead. Claude's exact accounting may differ.
    """
    return sum((len(json.dumps(rec.get("message", {}), ensure_ascii=False).encode("utf-8")) + 1) // 2 + 80
               for rec in records)


def excerpt(s: str, chars: int) -> str:
    if len(s) <= chars:
        return s
    front = max(0, chars // 3)
    back = max(0, chars - front)
    return s[:front] + "\n[... historical content omitted ...]\n" + s[-back:]


def bound_record(rec: dict, max_tool_chars: int) -> tuple[dict, bool]:
    """Bound large historical tool traffic without breaking use/result pairing."""
    r = copy.deepcopy(rec)
    trimmed = False
    # Claude Code's duplicate tool-result UI mirror is not needed for model replay.
    if "toolUseResult" in r:
        r.pop("toolUseResult")
    msg = r.get("message") or {}
    content = msg.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                value = block.get("content")
                if isinstance(value, str) and len(value) > max_tool_chars:
                    block["content"] = excerpt(value, max_tool_chars)
                    trimmed = True
                elif isinstance(value, list):
                    # Preserve block shape and identifiers; trim textual parts.
                    for part in value:
                        if isinstance(part, dict) and isinstance(part.get("text"), str) and len(part["text"]) > max_tool_chars:
                            part["text"] = excerpt(part["text"], max_tool_chars)
                            trimmed = True
            elif block.get("type") == "tool_use":
                args = block.get("input")
                # Keep the input dict structure/tool name, cap only extremely
                # large string arguments such as shell commands.
                if isinstance(args, dict):
                    for k, v in args.items():
                        if isinstance(v, str) and len(v) > max_tool_chars:
                            args[k] = excerpt(v, max_tool_chars)
                            trimmed = True
            elif block.get("type") == "text" and isinstance(block.get("text"), str):
                if len(block["text"]) > max_tool_chars * 3:
                    block["text"] = excerpt(block["text"], max_tool_chars * 3)
                    trimmed = True
    elif r.get("isMeta") and isinstance(content, str) and len(content) > max_tool_chars:
        msg["content"] = excerpt(content, max_tool_chars)
        trimmed = True
    return r, trimmed


def human_prompt(rec: dict) -> str | None:
    if rec.get("type") != "user" or rec.get("isMeta"):
        return None
    content = (rec.get("message") or {}).get("content")
    return content if isinstance(content, str) and content.strip() else None


def _note(template: dict, body: str) -> dict:
    r = copy.deepcopy(template)
    r["uuid"] = str(uuid.uuid4())
    r["type"] = "user"
    r["message"] = {"role": "user", "content": body}
    r["isMeta"] = True
    r.pop("toolUseResult", None)
    return r


def _omitted_note(prefix: list[dict], clipped: int) -> str:
    requests = [p.replace("\n", " ")[:200] for r in prefix if (p := human_prompt(r))]
    requests = requests[-12:]
    report = (
        "CONTEXT-BOUNDED CODEX IMPORT (historical excerpt, NOT a complete transcript).\n"
        "Earlier Codex turns were intentionally left out to make this session usable.\n"
        "Tool logs may be shortened. The original history remains under ~/.codex/sessions/;\n"
        "consult the repository and source rollouts before assuming old decisions.\n"
        f"Excluded {len(prefix)} earlier records. Shortened {clipped} large message/tool blocks.\n"
    )
    if requests:
        report += "\nSelected earlier user requests (excerpts, newest last):\n" + "\n".join(
            "- " + item for item in requests
        )
    return report


def fit_context(records: list[dict], budget: int, max_tool_chars: int = 3000, *,
                historical_summary: str | None = None,
                summary_reserve_tokens: int = 0) -> tuple[list[dict], dict]:
    """Keep the most recent complete user-turn groups under a rough token budget.

    Each group starts at a real user message. Paired tool use/results remain in
    their group; a pathological oversized last group falls back to a text-only
    minimal continuation instead of emitting an invalid half-tool sequence.
    """
    if budget < 5000:
        raise ValueError("context budget must be at least 5000 tokens")
    if summary_reserve_tokens < 0 or summary_reserve_tokens >= budget - 2000:
        raise ValueError("summary reserve must leave at least 2000 tokens for history")
    if max_tool_chars < 100:
        raise ValueError("max_tool_chars must be at least 100")
    if not records:
        return [], {"before": 0, "after": 0, "omitted": 0, "trimmed": 0}

    original_size = estimate_tokens(records)
    bounded = []
    clipped = 0
    for rec in records:
        nr, changed = bound_record(rec, max_tool_chars)
        bounded.append(nr)
        clipped += int(changed)

    prelude = []
    groups: list[list[dict]] = []
    for rec in bounded:
        if human_prompt(rec) is not None:
            groups.append([rec])
        elif groups:
            groups[-1].append(rec)
        else:
            prelude.append(rec)

    # Include a short provenance banner, but no old machine preamble.
    banner = next((r for r in prelude if r.get("isMeta") and
                   "Imported from OpenAI Codex" in str((r.get("message") or {}).get("content", ""))), None)
    if not groups:
        raise ValueError("no human message found to construct a working context")

    # Reserve a few thousand estimated tokens for the provenance/omission note.
    reserve = max(min(6000, max(1500, budget // 12)),
                  summary_reserve_tokens + 2000 if summary_reserve_tokens else 0)
    limit = budget - reserve
    selected = []
    cost = 0
    for group in reversed(groups):
        g_cost = estimate_tokens(group)
        if cost + g_cost > limit:
            break
        selected.insert(0, group)
        cost += g_cost

    if not selected:
        # The latest single user turn can itself contain more tool output than
        # the budget. Retain the human request and final assistant narrative,
        # but omit historical tool traffic rather than breaking tool pairs.
        last = groups[-1]
        texts = [r for r in last if r.get("type") == "assistant" and
                 any(b.get("type") == "text" for b in (r.get("message") or {}).get("content", [])
                     if isinstance(b, dict))]
        selected = [[last[0]] + texts[-2:]]
        keep = selected[0]
        if estimate_tokens(keep) > limit:
            # Extremely large pasted human prompts are also bounded, clearly
            # marked as abbreviated; all original bytes still exist in Codex.
            for r in keep:
                msg = r.get("message") or {}
                if isinstance(msg.get("content"), str):
                    msg["content"] = excerpt(msg["content"], max(1000, limit // 4))
                elif isinstance(msg.get("content"), list):
                    for part in msg["content"]:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            part["text"] = excerpt(part["text"], max(1000, limit // 4))
        if estimate_tokens(keep) > limit:
            raise ValueError("latest turn cannot fit in the context budget")

    kept_ids = {id(r) for group in selected for r in group}
    omitted = [r for group in groups for r in group if id(r) not in kept_ids]
    omitted = prelude + omitted
    body = _omitted_note(omitted, clipped)
    if historical_summary:
        body += "\n\n" + historical_summary
    note = _note(selected[0][0], body)
    result = ([banner] if banner is not None else []) + [note] + [
        r for group in selected for r in group
    ]
    for i, rec in enumerate(result):
        rec["parentUuid"] = result[i-1]["uuid"] if i else None

    after = estimate_tokens(result)
    if after > budget:
        raise ValueError(f"estimated working history {after} exceeds budget {budget}")
    return result, {"before": original_size, "after": after,
                    "omitted": len(omitted), "retained": sum(len(g) for g in selected),
                    "trimmed": clipped, "summary_estimate": estimate_tokens([note])}
