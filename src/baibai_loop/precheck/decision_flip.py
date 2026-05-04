"""Detect ``decision: skipped → accepted`` flips lacking an ``overrides`` entry.

Walks every ``records/04-research/**/*.md`` and compares the front-matter
``decision`` against the previous git commit of the same file. When the
current decision is ``accepted`` and the prior decision was ``skipped`` /
``pending`` (or absent), the file must declare ``overrides[].type =
'decision_flip'``. Otherwise we surface a finding mirrored after AP-09.

The check needs git history, so it lives outside ``baibai-loop-validate`` (a
pure-content checker) and inside ``baibai-loop-precheck`` (which is allowed to
shell out). Files outside a git working tree are silently skipped.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # nosec B404
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

_FRONT_MATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_FLIP_TYPE = "decision_flip"
_GIT_PATH = shutil.which("git")


@dataclass(frozen=True, slots=True)
class DecisionFlipFinding:
    """A flip-related precheck finding (mirrors precheck.OutlookFinding shape)."""

    severity: str
    target: Path
    code: str
    message: str
    location: str
    token: str = ""


def scan_research_decision_flips(
    research_root: Path, *, repo_root: Path
) -> list[DecisionFlipFinding]:
    if not research_root.is_dir() or not (repo_root / ".git").exists() or not _GIT_PATH:
        return []
    findings: list[DecisionFlipFinding] = []
    for path in sorted(research_root.rglob("*.md")):
        if not path.is_file():
            continue
        rel = path.resolve().relative_to(repo_root.resolve())
        finding = _check_one_file(repo_root, path, rel)
        if finding is not None:
            findings.append(finding)
    return findings


def _check_one_file(repo_root: Path, path: Path, rel: Path) -> DecisionFlipFinding | None:
    current_front = _parse_front_matter(path.read_text(encoding="utf-8"))
    if current_front is None:
        return None
    current_decision = current_front.get("decision")
    if current_decision != "accepted":
        return None
    prev_text = _previous_committed_text(repo_root, rel)
    if prev_text is None:
        return None
    prev_front = _parse_front_matter(prev_text)
    if prev_front is None:
        return None
    prev_decision = prev_front.get("decision")
    if prev_decision == "accepted":
        return None
    overrides = current_front.get("overrides")
    if isinstance(overrides, list):
        for entry in overrides:
            if isinstance(entry, dict) and entry.get("type") == _FLIP_TYPE:
                return None
    return DecisionFlipFinding(
        severity="warning",
        target=path,
        code="precheck.decision-flip-without-override",
        message=(
            f"decision flipped from {prev_decision!r} → 'accepted' between commits "
            f"but overrides[].type={_FLIP_TYPE!r} is missing"
        ),
        location="overrides",
    )


def _parse_front_matter(text: str) -> Mapping[str, object] | None:
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return None
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if isinstance(loaded, Mapping):
        return loaded
    return None


def _previous_committed_text(repo_root: Path, rel: Path) -> str | None:
    """Return the file content from the commit before HEAD's most recent change."""
    if not _GIT_PATH:
        return None
    rel_str = rel.as_posix()
    try:
        log = subprocess.run(  # nosec B603
            [
                _GIT_PATH,
                "-C",
                str(repo_root),
                "log",
                "--follow",
                "-2",
                "--pretty=format:%H",
                "--",
                rel_str,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError, FileNotFoundError:
        return None
    commits = [line for line in log.stdout.splitlines() if line.strip()]
    if len(commits) < 2:
        return None
    prev_commit = commits[1]
    try:
        show = subprocess.run(  # nosec B603
            [
                _GIT_PATH,
                "-C",
                str(repo_root),
                "show",
                f"{prev_commit}:{rel_str}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError, FileNotFoundError:
        return None
    return show.stdout
