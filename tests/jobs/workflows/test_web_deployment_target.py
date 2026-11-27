from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def _workflow() -> dict:
    return yaml.load(
        (ROOT / ".github/workflows/web.yml").read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )


def test_web_push_and_pull_request_validate_the_same_inputs():
    workflow = _workflow()
    events = workflow["on"]
    assert events["push"] == {"branches": ["main"]}
    assert events["pull_request"]["branches"] == ["main"]
    assert set(workflow["env"]["WEB_INPUTS"].split()) == {
        path.removesuffix("/**") for path in events["pull_request"]["paths"]
    }


def _step(job: str, step_id: str) -> dict[str, object]:
    return next(
        step for step in _workflow()["jobs"][job]["steps"] if step.get("id") == step_id
    )


def _git(directory: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(directory), *args], text=True
    ).strip()


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


def _run_step(
    runner: Path,
    job: str,
    step_id: str,
    output: Path,
    **step_env: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", str(_step(job, step_id)["run"])],
        cwd=runner,
        env={
            **os.environ,
            "WEB_INPUTS": _workflow()["env"]["WEB_INPUTS"],
            "GITHUB_OUTPUT": str(output),
            **step_env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _check_target(
    runner: Path, target: str, output: Path
) -> subprocess.CompletedProcess[str]:
    return _run_step(runner, "quality", "deploy-target", output, TARGET_SHA=target)


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
def test_deploy_target_compares_all_build_inputs(
    checkout, tmp_path, changed_path, current
):
    origin, runner, target = checkout
    after = target
    if changed_path is not None:
        changed = origin / changed_path
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_text("new input\n", encoding="utf-8")
        after = _commit(origin)
    output = tmp_path / "output"

    result = _check_target(runner, target, output)

    assert result.returncode == 0, result.stderr
    assert output.read_text(encoding="utf-8") == f"current={str(current).lower()}\n"

    # The push detector sees the same input surface, even without GitHub's path filter.
    scope = tmp_path / "scope"
    result = _run_step(
        runner, "scope", "changes", scope, BEFORE_SHA=target, TARGET_SHA=after
    )
    assert result.returncode == 0, result.stderr
    assert scope.read_text(encoding="utf-8") == f"web={str(not current).lower()}\n"


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
