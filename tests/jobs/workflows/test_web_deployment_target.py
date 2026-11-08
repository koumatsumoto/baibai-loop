from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def test_web_push_and_pull_request_validate_the_same_inputs():
    workflow = yaml.load(
        (ROOT / ".github/workflows/web.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    events = workflow["on"]
    assert set(events["push"]["paths"]) == set(events["pull_request"]["paths"])


def _target_step() -> dict[str, object]:
    workflow = yaml.load(
        (ROOT / ".github/workflows/web.yml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    return next(
        step for step in workflow["jobs"]["quality"]["steps"] if step.get("id") == "deploy-target"
    )


def _git(directory: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True).strip()


def _commit(directory: Path) -> str:
    _git(directory, "add", ".")
    _git(
        directory,
        "-c",
        "user.name=Workflow test",
        "-c",
        "user.email=workflow@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "Fixture change",
    )
    return _git(directory, "rev-parse", "HEAD")


@pytest.fixture
def checkout(tmp_path: Path) -> tuple[Path, Path, str]:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "README.md").write_text("initial\n", encoding="utf-8")
    target = _commit(origin)
    runner = tmp_path / "runner"
    _git(tmp_path, "clone", "-q", str(origin), str(runner))
    return origin, runner, target


def _check_target(runner: Path, target: str, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", str(_target_step()["run"])],
        cwd=runner,
        env={**os.environ, "TARGET_SHA": target, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("changed_path", "current"),
    [
        (None, True),
        ("README.md", True),
        ("web/frontend/src/App.tsx", False),
        ("web/edge/src/index.ts", False),
        ("web/contracts/read-models.schema.json", False),
        (".node-version", False),
        (".github/workflows/web.yml", False),
    ],
)
def test_deploy_target_compares_all_build_inputs(checkout, tmp_path, changed_path, current):
    origin, runner, target = checkout
    if changed_path is not None:
        changed = origin / changed_path
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_text("new input\n", encoding="utf-8")
        _commit(origin)
    output = tmp_path / "output"

    result = _check_target(runner, target, output)

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == f"current={str(current).lower()}\n"


@pytest.mark.parametrize("failure", ["fetch", "compare"])
def test_deploy_target_does_not_publish_after_git_failure(checkout, tmp_path, failure):
    _, runner, target = checkout
    if failure == "fetch":
        _git(runner, "remote", "set-url", "origin", str(tmp_path / "missing-origin"))
    else:
        target = "0" * 40
    output = tmp_path / "output"

    result = _check_target(runner, target, output)

    assert result.returncode != 0
    assert not output.exists()
