"""Verified source checkout identity for published lake artifacts."""

from __future__ import annotations

import subprocess  # nosec B404
from pathlib import Path


def source_repo_root() -> Path:
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / ".git").exists():
            return candidate
    raise RuntimeError("lake publication requires a source Git checkout")


def verified_git_commit() -> str:
    repo_root = source_repo_root()
    top_level = subprocess.run(
        ("git", "rev-parse", "--show-toplevel"),
        check=True,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )  # nosec B603
    if Path(top_level.stdout.strip()).resolve() != repo_root:
        raise RuntimeError("lake publication source repository identity is ambiguous")
    status = subprocess.run(
        ("git", "status", "--porcelain", "--untracked-files=no"),
        check=True,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )  # nosec B603
    if status.stdout.strip():
        raise RuntimeError("lake publication requires a clean tracked worktree")
    result = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )  # nosec B603
    commit = result.stdout.strip()
    if len(commit) != 40 or commit == "0" * 40:
        raise RuntimeError("lake publication requires a verifiable git commit")
    return commit
