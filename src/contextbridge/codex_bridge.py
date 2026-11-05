"""Talking to Codex through its supported app-server protocol.

`codex app-server` speaks newline-delimited JSON-RPC on stdio. Verified against
codex-cli 0.168 in an isolated CODEX_HOME:

* `externalAgentConfig/import` with an item of type SESSIONS imports exactly the
  sessions listed in `details.sessions`, not everything `detect` finds.
* Paths must sit under the Claude projects directory, else it reports
  `session_not_detected`. Subdirectories and old files are accepted.
* Importing unchanged content is a no-op (`successes` and `failures` both empty).
* After the source grows, re-importing appends the new turns to the same thread, unless
  the thread has native Codex work, in which case it is a no-op and nothing changes.
* The first human message titles the thread; the `title` field does not.
* `thread/archive` and `thread/unarchive` are reversible and report no error.

The protocol is marked experimental by OpenAI, so every failure here is expected to be
reported to the user rather than worked around.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class CodexUnavailable(RuntimeError):
    """Codex could not be reached; the workflow falls back to manual steps."""


@dataclass
class NativeImportResult:
    status: str                  # imported | no_change | failed
    thread_id: str | None = None
    message: str = ""


def find_codex() -> str | None:
    return shutil.which("codex")


class CodexBridge:
    """A short-lived app-server per operation; nothing stays resident."""

    def __init__(self, codex_home: Path, user_home: Path | None = None, executable: str | None = None,
                 timeout: float = 120):
        self.codex_home = Path(codex_home)
        self.user_home = user_home
        self.executable = executable
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._messages: queue.Queue = queue.Queue()
        self._backlog: list[dict] = []
        self._next_id = 0

    def available(self) -> bool:
        return bool(self.executable or find_codex())

    # -- process ----------------------------------------------------------------------
    def _start(self) -> None:
        exe = self.executable or find_codex()
        if not exe:
            raise CodexUnavailable("The `codex` command was not found on PATH.")
        env = dict(os.environ, CODEX_HOME=str(self.codex_home))
        if self.user_home:      # sandboxes redirect the Claude home the importer looks in
            env.update(USERPROFILE=str(self.user_home), HOME=str(self.user_home))
        try:
            self._proc = subprocess.Popen(
                [exe, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, text=True, encoding="utf-8",
                creationflags=_NO_WINDOW)
        except OSError as err:
            raise CodexUnavailable(f"Could not start `codex app-server`: {err}") from err
        threading.Thread(target=self._read, daemon=True).start()
        self._request("initialize", {"clientInfo": {"name": "contextbridge", "title": "ContextBridge",
                                                    "version": __version__},
                                     "capabilities": {"experimentalApi": True}})
        self._send({"method": "initialized"})

    def _read(self) -> None:
        proc = self._proc
        for line in proc.stdout:
            try:
                self._messages.put(json.loads(line))
            except ValueError:
                continue
        self._messages.put(None)

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.stdin.close()
            proc.wait(timeout=15)   # EOF on stdin ends the server cleanly
            if proc.stdout:
                proc.stdout.close()
        except (OSError, subprocess.TimeoutExpired):
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True, creationflags=_NO_WINDOW)
            else:
                proc.kill()

    def __enter__(self) -> "CodexBridge":
        self._start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop()

    def _send(self, message: dict) -> None:
        try:
            self._proc.stdin.write(json.dumps(message) + "\n")
            self._proc.stdin.flush()
        except OSError as err:
            raise CodexUnavailable(f"The Codex app-server stopped responding: {err}") from err

    def _wait_for(self, predicate, timeout: float) -> dict:
        for i, msg in enumerate(self._backlog):
            if predicate(msg):
                return self._backlog.pop(i)
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise CodexUnavailable("Timed out waiting for the Codex app-server.")
            try:
                msg = self._messages.get(timeout=left)
            except queue.Empty:
                raise CodexUnavailable("Timed out waiting for the Codex app-server.") from None
            if msg is None:
                raise CodexUnavailable("The Codex app-server exited unexpectedly.")
            if predicate(msg):
                return msg
            self._backlog.append(msg)

    def _request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"id": rid, "method": method, "params": params})
        reply = self._wait_for(lambda m: m.get("id") == rid and ("result" in m or "error" in m),
                               self.timeout)
        if "error" in reply:
            raise RuntimeError(f"Codex rejected {method}: {reply['error'].get('message', reply['error'])}")
        return reply["result"]

    # -- operations ---------------------------------------------------------------------
    def import_claude_session(self, path: Path, cwd: str, title: str = "") -> NativeImportResult:
        """Run Codex's own importer on exactly one Claude transcript."""
        item = {"itemType": "SESSIONS",
                "description": f"ContextBridge handoff of {Path(path).name}", "cwd": None,
                "details": {"sessions": [{"cwd": cwd, "path": str(path), "title": title or None}]}}
        with self:
            result = self._request("externalAgentConfig/import",
                                   {"migrationItems": [item], "source": "contextbridge",
                                    "providerId": "contextbridge"})
            import_id = result["importId"]
            done = self._wait_for(
                lambda m: m.get("method") == "externalAgentConfig/import/completed"
                and (m.get("params") or {}).get("importId") == import_id, self.timeout)
        for group in done["params"].get("itemTypeResults", []):
            if group.get("failures"):
                failure = group["failures"][0]
                return NativeImportResult("failed", message=failure.get("message")
                                          or failure.get("subErrorType") or "unknown failure")
            if group.get("successes"):
                return NativeImportResult("imported", thread_id=group["successes"][0].get("target"))
        return NativeImportResult(
            "no_change", message="Codex imported nothing: it reports no new content for this "
                                 "session, or the imported thread has its own newer work.")

    def archive_thread(self, thread_id: str) -> None:
        with self:
            self._request("thread/archive", {"threadId": thread_id})

    def unarchive_thread(self, thread_id: str) -> None:
        with self:
            self._request("thread/unarchive", {"threadId": thread_id})
