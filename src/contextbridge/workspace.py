"""Session history and workspace state are different things. Report the second one; never
change it. Every git call here is read-only."""
from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class Workspace:
    cwd: str = ""
    exists: bool = False
    is_repo: bool = False
    root: str = ""
    branch: str = ""
    head: str = ""
    head_subject: str = ""
    upstream: str = ""
    ahead: int = 0
    behind: int = 0
    modified: int = 0
    staged: int = 0
    untracked: int = 0
    operation: str = ""         # merge, rebase, bisect... left in progress
    notes: list[str] = field(default_factory=list)

    @property
    def dirty(self) -> bool:
        return bool(self.modified or self.staged or self.untracked)

    def to_dict(self) -> dict:
        return asdict(self)


def _git(cwd: str, *args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=8,
                             creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.rstrip() if out.returncode == 0 else None


def inspect_workspace(cwd: str, recorded_branch: str = "") -> Workspace:
    ws = Workspace(cwd=cwd)
    if not cwd:
        ws.notes.append("The session has no recorded working directory.")
        return ws
    path = Path(cwd)
    ws.exists = path.is_dir()
    if not ws.exists:
        ws.notes.append(f"The working directory no longer exists: {cwd}")
        return ws
    root = _git(cwd, "rev-parse", "--show-toplevel")
    if root is None:
        ws.notes.append("Not a Git repository; code state cannot be checked.")
        return ws
    ws.is_repo = True
    ws.root = str(Path(root))
    ws.branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or ""
    if ws.branch == "HEAD":
        ws.notes.append("The repository is on a detached HEAD.")
    log = _git(cwd, "log", "-1", "--format=%h%x00%s") or ""
    if "\0" in log:
        ws.head, ws.head_subject = log.split("\0", 1)
    ws.upstream = _git(cwd, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") or ""
    if ws.upstream:
        counts = _git(cwd, "rev-list", "--left-right", "--count", f"HEAD...{ws.upstream}")
        if counts and len(counts.split()) == 2:
            ws.ahead, ws.behind = (int(n) for n in counts.split())
    status = _git(cwd, "status", "--porcelain=v1") or ""
    for line in status.splitlines():
        code = line[:2]
        if code == "??":
            ws.untracked += 1
        else:
            if code[0] not in " ?":
                ws.staged += 1
            if code[1] != " ":
                ws.modified += 1
    git_dir = _git(cwd, "rev-parse", "--absolute-git-dir")
    if git_dir:
        for marker, label in (("MERGE_HEAD", "merge"), ("rebase-merge", "rebase"),
                              ("rebase-apply", "rebase"), ("CHERRY_PICK_HEAD", "cherry-pick"),
                              ("BISECT_LOG", "bisect"), ("REVERT_HEAD", "revert")):
            if (Path(git_dir) / marker).exists():
                ws.operation = label
                ws.notes.append(f"A {label} is in progress in this repository.")
                break
    if ws.dirty:
        ws.notes.append(f"Uncommitted changes: {ws.modified} modified, {ws.staged} staged, "
                        f"{ws.untracked} untracked. They stay in the working tree; the "
                        "transcript does not carry them.")
    if ws.behind:
        ws.notes.append(f"The branch is {ws.behind} commit(s) behind {ws.upstream}.")
    if recorded_branch and ws.branch and recorded_branch != ws.branch and recorded_branch != "HEAD":
        ws.notes.append(f"The session last worked on branch '{recorded_branch}', but the "
                        f"repository is on '{ws.branch}' now.")
    return ws


def compare_workspaces(source: Workspace, dest_cwd: str) -> list[str]:
    """What the destination agent will and will not see in the working tree."""
    if not dest_cwd or not source.cwd:
        return []
    if _same_path(source.cwd, dest_cwd):
        return ["Both agents use the same working directory, so they see the same files."]
    other = inspect_workspace(dest_cwd)
    if source.root and other.root and _same_path(source.root, other.root):
        return [f"Both sessions use repository {source.root}, in different subdirectories."]
    if source.root and other.is_repo:
        return [f"The destination uses a different checkout: {other.root} "
                f"({other.branch or 'unknown branch'}). Code changes will not carry across."]
    return [f"The destination uses a different directory: {dest_cwd}."]


def _same_path(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return a == b
