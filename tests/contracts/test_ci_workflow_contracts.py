from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"

_BANDIT = (
    "uv run bandit -c pyproject.toml -q -r "
    "engine/src/baibai_engine web/backend/src/baibai_web jobs/src/baibai_jobs tools"
)
# A scanner is weakened by editing its command, not by deleting its step: a lowered
# --audit-level, a narrowed Bandit root, an --ignore-vuln or a trailing `|| true`
# all leave a green step whose name still reads like the gate it used to be.
_SCANNER_COMMANDS = {
    ("ci.yml", "quality", "Bandit"): _BANDIT,
}


def _workflow(name: str) -> dict[str, object]:
    loaded = yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _steps(name: str, job: str) -> list[dict[str, object]]:
    jobs = _workflow(name)["jobs"]
    assert isinstance(jobs, dict)
    selected = jobs[job]
    assert isinstance(selected, dict)
    steps = selected["steps"]
    assert isinstance(steps, list)
    return steps


def _by_name(name: str, job: str) -> dict[str, dict[str, object]]:
    return {str(step["name"]): step for step in _steps(name, job) if "name" in step}


def test_ci_answers_pull_requests_and_manual_dispatch_only() -> None:
    """Use PR and manual runs; do not duplicate the same tree on every push.

    A changed base or merge strategy can change the tree, so a previous PR result
    is not an unconditional guarantee about a later main commit.
    """
    triggers = _workflow("ci.yml")["on"]
    assert isinstance(triggers, dict)

    assert set(triggers) == {"pull_request", "workflow_dispatch"}
    pull_request = triggers["pull_request"]
    assert isinstance(pull_request, dict)
    assert pull_request["branches"] == ["main"]
    assert "paths" not in pull_request
    assert "paths-ignore" not in pull_request


def test_python_quality_gates_share_one_runner() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    assert isinstance(jobs, dict)
    names = set(_by_name("ci.yml", "quality"))

    assert set(jobs) == {"quality"}
    assert {
        "Ruff format",
        "Ruff lint",
        "Type check",
        "Import contracts",
        "Drift gates",
        "Tests with coverage",
        "Bandit",
        "Dependency audit",
        "Build dependency audit",
    } <= names


def test_ci_pins_an_explicit_worker_count() -> None:
    """Keep the selected four-worker budget explicit, not host-dependent.

    Historical timings do not prove it remains fastest on every runner or suite.
    """
    run = str(_by_name("ci.yml", "quality")["Tests with coverage"]["run"])

    assert "-n 4" in run
    assert "--cov" in run
    assert "tests/" not in run  # the gate is the whole suite, not a chosen file


@pytest.mark.parametrize(("target", "command"), sorted(_SCANNER_COMMANDS.items()))
def test_each_scanner_still_asks_its_full_question(
    target: tuple[str, str, str], command: str
) -> None:
    workflow, job, step = target

    assert _by_name(workflow, job)[step]["run"] == command


@pytest.mark.parametrize(("workflow", "job"), [("ci.yml", "quality"), ("security.yml", "audit")])
def test_the_python_dependency_audit_suppresses_nothing(workflow: str, job: str) -> None:
    by_name = _by_name(workflow, job)
    run = str(by_name["Dependency audit"]["run"])

    assert "uv run pip-audit" in run
    assert "--all-groups" in run
    assert "--ignore-vuln" not in run
    assert "|| true" not in run
    build_run = str(by_name["Build dependency audit"]["run"])
    assert 'config["build-system"]["requires"]' in build_run
    assert "uv run pip-audit" in build_run
    assert "--ignore-vuln" not in build_run
    assert "|| true" not in build_run
    # Separate steps need an explicit status condition to survive the prior failure.
    for scanner in ("Dependency audit", "Build dependency audit"):
        assert by_name[scanner]["if"] == "${{ !cancelled() }}"
    assert "build-requirements.txt" not in run
    assert "uv export" not in build_run


def test_scanners_are_asked_even_after_an_earlier_gate_fails() -> None:
    """Sharing the runner does not skip scanners after another gate fails."""
    by_name = _by_name("ci.yml", "quality")

    for scanner in ("Bandit", "Dependency audit", "Build dependency audit"):
        assert by_name[scanner]["if"] == "${{ !cancelled() }}"


@pytest.mark.parametrize("workflow_name", ["ci.yml", "security.yml"])
def test_jobs_have_bounded_timeouts(workflow_name: str) -> None:
    jobs = _workflow(workflow_name)["jobs"]
    assert isinstance(jobs, dict)

    for job in jobs.values():
        assert isinstance(job, dict)
        assert int(str(job["timeout-minutes"])) <= 20


def test_weekly_sweep_asks_only_what_a_pull_request_cannot() -> None:
    """Recheck external advisories and the current main workflow trust boundary.

    Pinned source scanning remains in PR CI; this does not assert that every
    runtime, environment, or remote dependency is immutable.
    """
    workflow = _workflow("security.yml")
    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    names = set(_by_name("security.yml", "audit"))

    assert set(jobs) == {"audit"}
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "17 3 * * 1"}]
    assert names == {
        "Sync",
        "Workflow trust boundary",
        "Dependency audit",
        "Build dependency audit",
    }


def test_weekly_dependency_audits_run_after_an_earlier_finding() -> None:
    by_name = _by_name("security.yml", "audit")
    for name in ("Dependency audit", "Build dependency audit"):
        assert by_name[name]["if"] == "${{ !cancelled() }}"
