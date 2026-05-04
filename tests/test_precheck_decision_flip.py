from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from baibai_loop.precheck.decision_flip import scan_research_decision_flips


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "--allow-empty", "-m", "init", "--no-gpg-sign")
    return repo


def _write_research(repo: Path, *, decision: str, overrides_yaml: str | None = None) -> Path:
    research_dir = repo / "records/04-research/2026/05"
    research_dir.mkdir(parents=True, exist_ok=True)
    path = research_dir / "2026-05-04-9682-test.md"
    overrides_block = overrides_yaml or ""
    path.write_text(
        f'---\nticker: "9682"\ndecision: {decision}\n{overrides_block}---\n# body\n',
        encoding="utf-8",
    )
    return path


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message, "--no-gpg-sign")


def test_no_findings_when_decision_unchanged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_research(repo, decision="accepted")
    _commit(repo, "first")
    _write_research(repo, decision="accepted")
    (repo / "records/04-research/2026/05/2026-05-04-9682-test.md").write_text(
        '---\nticker: "9682"\ndecision: accepted\nnote: tweaked\n---\n# body\n',
        encoding="utf-8",
    )
    _commit(repo, "second")
    findings = scan_research_decision_flips(repo / "records/04-research", repo_root=repo)
    assert findings == []


def test_skipped_to_accepted_flip_without_override_is_flagged(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    _write_research(repo, decision="skipped")
    _commit(repo, "first skipped")
    _write_research(repo, decision="accepted")
    _commit(repo, "flip to accepted without override")
    findings = scan_research_decision_flips(repo / "records/04-research", repo_root=repo)
    codes = {f.code for f in findings}
    assert "precheck.decision-flip-without-override" in codes


def test_skipped_to_accepted_flip_with_override_passes(tmp_path: Path) -> None:
    overrides_yaml = (
        "overrides:\n"
        "  - type: decision_flip\n"
        '    prior_state_ref: "abc:research"\n'
        '    prior_state: "skipped"\n'
        '    new_state: "accepted"\n'
        '    reason: "tested"\n'
    )
    repo = _init_repo(tmp_path)
    _write_research(repo, decision="skipped")
    _commit(repo, "first skipped")
    _write_research(repo, decision="accepted", overrides_yaml=overrides_yaml)
    _commit(repo, "flip with override")
    findings = scan_research_decision_flips(repo / "records/04-research", repo_root=repo)
    codes = {f.code for f in findings}
    assert "precheck.decision-flip-without-override" not in codes


def test_outside_git_repo_is_silent(tmp_path: Path) -> None:
    research_root = tmp_path / "records/04-research"
    research_root.mkdir(parents=True)
    findings = scan_research_decision_flips(research_root, repo_root=tmp_path)
    assert findings == []


@pytest.mark.parametrize("prior", ["pending", "skipped"])
def test_pending_or_skipped_to_accepted_flips_are_detected(tmp_path: Path, prior: str) -> None:
    repo = _init_repo(tmp_path)
    _write_research(repo, decision=prior)
    _commit(repo, f"first {prior}")
    _write_research(repo, decision="accepted")
    _commit(repo, "flip")
    findings = scan_research_decision_flips(repo / "records/04-research", repo_root=repo)
    codes = {f.code for f in findings}
    assert "precheck.decision-flip-without-override" in codes
