"""Where everything lives. A single value object so tests and sandboxes never touch
the real ~/.codex or ~/.claude by accident."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import codex_import as ci


@dataclass(frozen=True)
class Roots:
    codex_home: Path
    claude_home: Path
    app_home: Path

    @classmethod
    def default(cls) -> "Roots":
        home = Path.home()
        return cls(
            codex_home=Path(os.environ.get("CODEX_HOME") or home / ".codex"),
            claude_home=Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude"),
            app_home=Path(os.environ.get("CONTEXTBRIDGE_HOME") or home / ".contextbridge"),
        )

    def activate(self) -> None:
        """Point the shared converter at these roots."""
        ci.configure_paths(self.codex_home, self.claude_home)

    # -- Codex ---------------------------------------------------------------------
    @property
    def codex_sessions(self) -> Path:
        return self.codex_home / "sessions"

    @property
    def codex_archived(self) -> Path:
        return self.codex_home / "archived_sessions"

    @property
    def codex_ledger(self) -> Path:
        return self.codex_home / "external_agent_session_imports.json"

    @property
    def codex_state_db(self) -> Path:
        return self.codex_home / "state_5.sqlite"

    # -- Claude --------------------------------------------------------------------
    @property
    def claude_projects(self) -> Path:
        return self.claude_home / "projects"

    @property
    def claude_ledger(self) -> Path:
        return self.claude_home / "codex-imports.json"

    @property
    def claude_live(self) -> Path:
        return self.claude_home / "sessions"

    @property
    def handoff_project(self) -> Path:
        """Codex's importer only accepts paths under ~/.claude/projects, so delta
        transcripts wait here during an import. It is hidden from the browser."""
        return self.claude_projects / "contextbridge-handoffs"

    # -- ContextBridge -------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.app_home / "lineage.db"

    @property
    def reports_dir(self) -> Path:
        return self.app_home / "reports"

    @property
    def backups_dir(self) -> Path:
        return self.app_home / "backups"

    @property
    def deltas_dir(self) -> Path:
        return self.app_home / "deltas"

    @property
    def archive_dir(self) -> Path:
        return self.app_home / "archive"

    @property
    def log_path(self) -> Path:
        return self.app_home / "contextbridge.log"
