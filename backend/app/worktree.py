"""Git worktree isolation for pipelines.

A pipeline runs its agents in a DEDICATED worktree + branch so their edits and
commits never touch the user's main checkout. The user reviews the diff and
merges (or throws the branch away) themselves.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class WorktreeError(RuntimeError):
    pass


def _git(root: str, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", root, *args],
                          capture_output=True, text=True, timeout=timeout)


def is_git_repo(root: str) -> bool:
    r = _git(root, "rev-parse", "--is-inside-work-tree")
    return r.returncode == 0 and r.stdout.strip() == "true"


def current_branch(root: str) -> str:
    r = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else "HEAD"


def default_path(root: str, pipeline_id: str) -> str:
    """A sibling dir next to the repo: <name>-pipe-<id8>. A worktree must live
    OUTSIDE the main working tree, so we never nest it inside `root`."""
    p = Path(root)
    return str(p.parent / f"{p.name}-pipe-{pipeline_id[:8]}")


def create(root: str, branch: str, path: str, base: str | None = None) -> None:
    """git worktree add -b <branch> <path> [base]. Raises on failure."""
    if not is_git_repo(root):
        raise WorktreeError(f"not a git repo: {root}")
    if Path(path).exists():
        raise WorktreeError(f"worktree path already exists: {path}")
    args = ["worktree", "add", "-b", branch, path]
    if base:
        args.append(base)
    r = _git(root, *args)
    if r.returncode != 0:
        raise WorktreeError(f"worktree add failed: {r.stderr.strip() or r.stdout.strip()}")


def remove(root: str, path: str) -> None:
    """Best-effort cleanup: drop the worktree (force, in case it's dirty). The
    BRANCH is deliberately left behind so the user can still inspect/merge it."""
    _git(root, "worktree", "remove", "--force", path)
