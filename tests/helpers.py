"""Synthetic Codex and Claude homes, and a stand-in for Codex's importer.

FakeCodexBridge mirrors what `tests/test_real_codex.py` observes the real importer do:
no-op on unchanged content, append on growth, no-op once the thread has native work,
refusal for paths outside the Claude projects directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from contextbridge import converter, desktop_register  # noqa: E402
from contextbridge.codex_bridge import NativeImportResult  # noqa: E402
from contextbridge.handoff import HandoffOptions  # noqa: E402
from contextbridge.models import Provider  # noqa: E402
from contextbridge.paths import Roots  # noqa: E402
from contextbridge.service import ContextBridge  # noqa: E402


def external_import_end(recs: list[tuple[int, dict]]) -> int | None:
    """Offset past the last turn Codex replayed from a Claude session."""
    end = None
    for off, rec in recs:
        payload = rec.get("payload") or {}
        if (rec.get("type") == "event_msg" and payload.get("type") == "task_complete"
                and str(payload.get("turn_id", "")).startswith(converter.EXTERNAL_TURN_PREFIX)):
            end = off
    return end


class Clock:
    """Strictly increasing transcript timestamps."""

    def __init__(self):
        self.n = 0

    def next(self) -> str:
        self.n += 1
        return f"2026-11-01T{(self.n // 3600) % 24:02d}:{(self.n // 60) % 60:02d}:{self.n % 60:02d}.000Z"


class FakeCodexBridge:
    def __init__(self, sandbox: "Sandbox"):
        self.box = sandbox
        self.imports: list[str] = []
        self.archived: list[str] = []
        self.unavailable = False
        self.force_no_change = False

    def import_claude_session(self, path, cwd, title=""):
        from contextbridge.codex_bridge import CodexUnavailable
        if self.unavailable:
            raise CodexUnavailable("codex not found (test)")
        if self.force_no_change:
            return NativeImportResult("no_change", message="Codex imported nothing")
        path = Path(path)
        projects = self.box.claude_home / "projects"
        if projects not in path.parents:
            return NativeImportResult("failed", message="session_not_detected")
        self.imports.append(str(path))
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        ledger = self.box.read_codex_ledger()
        record = next((r for r in ledger["records"]
                       if converter.normalize_path(r["source_path"]) == str(path)), None)
        records = [json.loads(line) for line in data.splitlines() if line.strip()]
        chain = [r for r in records if r.get("type") in ("user", "assistant")
                 and not r.get("isSidechain") and not r.get("isMeta")]
        if record is None:
            thread = self.box.new_thread_id()
            self.box.codex_rollout(thread, cwd=cwd, source="vscode")
            done = 0
        else:
            if record["content_sha256"] == digest:
                return NativeImportResult("no_change")
            thread = record["imported_thread_id"]
            rollout = self.box.rollout_path(thread)
            scanned = rollout.read_bytes()
            end = external_import_end(converter.records_from(scanned))
            if converter.records_from(scanned, end or 0):
                return NativeImportResult("no_change")
            done = sum(1 for r in converter.records_from(scanned)
                       if r[1].get("type") == "response_item"
                       and (r[1].get("payload") or {}).get("type") == "message")
        rows = []
        turn = sum(1 for r in self.box.rollout_records(thread)
                   if (r.get("payload") or {}).get("type") == "task_complete") + 1
        for rec in chain[done:]:
            content = (rec.get("message") or {}).get("content")
            if rec["type"] == "user":
                if isinstance(content, list):
                    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                        continue
                    content = "\n".join(b.get("text", "") for b in content)
                rows.append(self.box.ev_task_started(f"{converter.EXTERNAL_TURN_PREFIX}-{turn}"))
                rows.append(self.box.ev_user(content))
                rows.append(self.box.ev_done(f"{converter.EXTERNAL_TURN_PREFIX}-{turn}"))
                turn += 1
            else:
                text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict)
                                 and b.get("type") == "text") if isinstance(content, list) else str(content)
                if text:
                    rows.append(self.box.ev_assistant(text))
                    rows.append(self.box.ev_done(f"{converter.EXTERNAL_TURN_PREFIX}-{turn - 1}"))
        self.box.append_rollout(thread, rows)
        ledger["records"] = [r for r in ledger["records"]
                             if converter.normalize_path(r["source_path"]) != str(path)]
        ledger["records"].append({
            "source_path": "\\\\?\\" + str(path) if sys.platform == "win32" else str(path),
            "content_sha256": digest, "imported_thread_id": thread, "imported_at": 1793902720,
            "source_modified_at": 0, "connector_names": [], "title": title})
        self.box.codex_ledger.write_text(json.dumps(ledger), encoding="utf-8")
        return NativeImportResult("imported", thread_id=thread)

    def archive_thread(self, thread_id):
        self.archived.append(thread_id)

    def unarchive_thread(self, thread_id):
        self.archived.remove(thread_id)


class Sandbox:
    def __init__(self):
        # A child process may hold a file for a moment after exiting on Windows.
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self._tmp.name).resolve()
        self.codex_home = self.root / "home" / ".codex"
        self.claude_home = self.root / "home" / ".claude"
        self.app_home = self.root / "app"
        self.project = self.root / "project"
        self.project.mkdir()
        (self.codex_home / "sessions" / "2026" / "11" / "01").mkdir(parents=True)
        (self.claude_home / "projects").mkdir(parents=True)
        self.codex_ledger = self.codex_home / "external_agent_session_imports.json"
        self.claude_ledger = self.claude_home / "codex-imports.json"
        self.desktop_support = self.root / "desktop"
        self.clock = Clock()
        self.counter = 0
        self.bridge = FakeCodexBridge(self)
        self.original = (converter.CODEX_SESSIONS, converter.CODEX_IMPORT_LEDGER, converter.CLAUDE_PROJECTS,
                         converter.LEDGER, converter.BACKUPS)

    def app(self, **kw) -> ContextBridge:
        roots = Roots(self.codex_home, self.claude_home, self.app_home, self.desktop_support)
        kw.setdefault("version_probe", lambda: "2.1.300")
        kw.setdefault("desktop_probe", lambda: False)
        return ContextBridge(roots, bridge_factory=lambda: self.bridge, **kw)

    def close(self) -> None:
        (converter.CODEX_SESSIONS, converter.CODEX_IMPORT_LEDGER, converter.CLAUDE_PROJECTS,
         converter.LEDGER, converter.BACKUPS) = self.original
        desktop_register.configure_paths(None)
        self._tmp.cleanup()

    # -- Claude Desktop ------------------------------------------------------------------
    def desktop_card(self, account: str, workspace: str, cli_id: str, title: str = "A session",
                     **fields) -> Path:
        reg = self.desktop_support / "claude-code-sessions" / account / workspace
        reg.mkdir(parents=True, exist_ok=True)
        path = reg / f"local_{uuid.uuid4()}.json"
        path.write_text(json.dumps({"sessionId": path.stem, "cliSessionId": cli_id, "title": title,
                                    **fields}), encoding="utf-8")
        return path

    def set_active_account(self, account: str) -> None:
        (self.desktop_support / "config.json").write_text(
            json.dumps({"lastKnownAccountUuid": account}), encoding="utf-8")

    # -- Codex side ----------------------------------------------------------------------
    def new_thread_id(self) -> str:
        self.counter += 1
        return f"01a1{self.counter:04x}-0000-7000-8000-{uuid.uuid4().hex[:12]}"

    def rollout_path(self, thread: str, rollout: str | None = None) -> Path:
        base = self.codex_home / "sessions" / "2026" / "11" / "01"
        hits = sorted((self.codex_home / "sessions").rglob(f"rollout-*-{thread}*"))
        if rollout is None and hits:
            return hits[-1]
        suffix = f"_{rollout}" if rollout else ""
        return base / f"rollout-2026-11-01T10-00-{self.counter % 60:02d}-{thread}{suffix}.jsonl"

    def codex_rollout(self, thread: str | None = None, *, cwd=None, source="vscode",
                      rollout=None, base=None, thread_source=None) -> str:
        thread = thread or self.new_thread_id()
        meta = {"id": thread, "session_id": thread, "timestamp": self.clock.next(),
                "cwd": str(cwd or self.project), "originator": "Codex Desktop", "source": source}
        if base:
            meta["history_base"] = base
        if thread_source:
            meta["thread_source"] = thread_source
        path = self.rollout_path(thread, rollout) if rollout or base else self.rollout_path(thread)
        if rollout is None and not base:
            path = self.codex_home / "sessions" / "2026" / "11" / "01" / \
                f"rollout-2026-11-01T10-00-{self.counter % 60:02d}-{thread}.jsonl"
        path.write_bytes(json.dumps({"timestamp": meta["timestamp"], "type": "session_meta",
                                     "payload": meta}).encode() + b"\n")
        return thread

    def ev_user(self, text: str) -> dict:
        return {"timestamp": self.clock.next(), "type": "response_item",
                "payload": {"type": "message", "role": "user",
                            "content": [{"type": "input_text", "text": text}]}}

    def ev_assistant(self, text: str) -> dict:
        return {"timestamp": self.clock.next(), "type": "response_item",
                "payload": {"type": "message", "role": "assistant",
                            "content": [{"type": "output_text", "text": text}]}}

    def ev_task_started(self, turn: str) -> dict:
        return {"timestamp": self.clock.next(), "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": turn}}

    def ev_done(self, turn: str) -> dict:
        return {"timestamp": self.clock.next(), "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": turn}}

    def append_rollout(self, thread: str, rows: list[dict]) -> None:
        with self.rollout_path(thread).open("ab") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n")

    def rollout_records(self, thread: str) -> list[dict]:
        return [rec for _, rec in converter.records_from(converter.rollout_bytes(self.rollout_path(thread)))]

    def codex_turn(self, thread: str, human: str, answer: str, *, tool: bool = False) -> None:
        """A native Codex turn."""
        turn = uuid.uuid4().hex
        rows = [self.ev_task_started(turn), self.ev_user(human)]
        if tool:
            call = uuid.uuid4().hex[:8]
            rows.append({"timestamp": self.clock.next(), "type": "response_item",
                         "payload": {"type": "custom_tool_call", "call_id": call,
                                     "name": "exec", "input": "git status"}})
            rows.append({"timestamp": self.clock.next(), "type": "response_item",
                         "payload": {"type": "custom_tool_call_output", "call_id": call,
                                     "output": [{"type": "input_text", "text": "clean"}]}})
        rows += [self.ev_assistant(answer), self.ev_done(turn)]
        self.append_rollout(thread, rows)

    def age(self, thread: str, seconds: int = 3600) -> None:
        """Make a rollout look settled rather than written a moment ago."""
        path = self.rollout_path(thread)
        stamp = path.stat().st_mtime - seconds
        os.utime(path, (stamp, stamp))

    def read_codex_ledger(self) -> dict:
        try:
            return json.loads(self.codex_ledger.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"records": [], "detected_connector_records": []}

    # -- Claude side ---------------------------------------------------------------------
    def claude_path(self, sid: str, cwd=None) -> Path:
        slug = converter.slug_for(str(cwd or self.project))
        return self.claude_home / "projects" / slug / f"{sid}.jsonl"

    def claude_session(self, *turns: tuple[str, str], sid: str | None = None, cwd=None) -> str:
        sid = sid or str(uuid.uuid4())
        path = self.claude_path(sid, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        for human, answer in turns:
            self.claude_turn(sid, human, answer, cwd=cwd)
        return sid

    def _claude_tail(self, path: Path) -> str | None:
        return converter.transcript_tail(path)["leaf"]

    def claude_turn(self, sid: str, human: str, answer: str, cwd=None, path: Path | None = None) -> None:
        """A live Claude Code turn; note the fields live records have and converted ones lack."""
        path = path or self.find_claude(sid)
        parent = self._claude_tail(path)
        user_id, assistant_id = str(uuid.uuid4()), str(uuid.uuid4())
        rows = [{"parentUuid": parent, "isSidechain": False, "userType": "external",
                 "promptId": str(uuid.uuid4()), "permissionMode": "default",
                 "origin": {"kind": "human"}, "cwd": str(cwd or self.project), "sessionId": sid,
                 "version": "2.1.300", "gitBranch": "main", "entrypoint": "cli", "type": "user",
                 "uuid": user_id, "timestamp": self.clock.next(),
                 "message": {"role": "user", "content": human}},
                {"parentUuid": user_id, "isSidechain": False, "userType": "external",
                 "cwd": str(cwd or self.project), "sessionId": sid, "version": "2.1.300",
                 "gitBranch": "main", "entrypoint": "cli", "type": "assistant",
                 "uuid": assistant_id, "timestamp": self.clock.next(),
                 "message": {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant",
                             "model": "claude-opus-5-5", "content": [{"type": "text", "text": answer}],
                             "stop_reason": "end_turn", "stop_sequence": None,
                             "usage": {"input_tokens": 1, "output_tokens": 1}}}]
        with path.open("ab") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n")

    def find_claude(self, sid: str) -> Path:
        hits = list((self.claude_home / "projects").glob(f"*/{sid}.jsonl"))
        assert hits, f"no transcript for {sid}"
        return hits[0]

    def claude_humans(self, sid: str) -> list[str]:
        """Text people typed in a transcript, in order, counting converted Codex turns."""
        out = []
        for line in self.find_claude(sid).read_bytes().splitlines():
            rec = json.loads(line)
            if rec.get("type") == "user" and not rec.get("isMeta"):
                content = rec["message"]["content"]
                if isinstance(content, str):
                    out.append(content)
        return out

    def codex_humans(self, thread: str) -> list[str]:
        out = []
        for rec in self.rollout_records(thread):
            p = rec.get("payload") or {}
            if rec.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                out.append(converter.text_of(p.get("content")))
        return out


class SandboxCase(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.close)
        self.app = self.box.app()
        self.addCleanup(self.app.store.close)

    def refresh(self):
        return self.app.refresh()

    def get(self, provider, sid):
        self.refresh()
        session = self.app.session(provider, sid)
        self.assertIsNotNone(session, f"{provider} {sid} not discovered")
        return session

    def options(self, **kw) -> HandoffOptions:
        kw.setdefault("context_budget", None)
        return HandoffOptions(**kw)

    def send(self, provider, sid, target, strategy=None, **options):
        session = self.get(provider, sid)
        plan = self.app.plan(session, target, self.options(**options), strategy)
        self.assertTrue(plan.can_execute, plan.blocked or plan.strategy)
        return self.app.execute(plan)


CODEX, CLAUDE = Provider.CODEX, Provider.CLAUDE
