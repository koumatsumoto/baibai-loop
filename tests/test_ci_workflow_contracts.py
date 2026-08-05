from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


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


@pytest.mark.parametrize("workflow_name", ["ci.yml", "security.yml"])
def test_no_gate_recheck_is_billed_for_a_tree_that_already_passed(workflow_name: str) -> None:
    """The merge ref a pull request checks is the tree that lands on main.

    Actions bills each job by the minute, so a second run over identical bytes is
    paid for and proves nothing new. main stays covered by the next pull request,
    whose own merge ref contains it, and by the daily batch that runs main.
    """
    assert "push" not in _workflow(workflow_name)["on"]


def test_web_keeps_the_main_push_that_publishes_production() -> None:
    triggers = _workflow("web.yml")["on"]
    assert isinstance(triggers, dict)

    assert "push" in triggers


def test_ci_answers_pull_requests_and_manual_dispatch_only() -> None:
    triggers = _workflow("ci.yml")["on"]
    assert isinstance(triggers, dict)

    assert set(triggers) == {"pull_request", "workflow_dispatch"}
    pull_request = triggers["pull_request"]
    assert isinstance(pull_request, dict)
    assert pull_request["branches"] == ["main"]
    assert "paths" not in pull_request
    assert "paths-ignore" not in pull_request


def test_every_python_gate_shares_the_one_billed_runner() -> None:
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
        "Brand asset contract",
        "Bandit",
        "Dependency audit",
        "Build package",
    } <= names


def test_coverage_is_measured_inside_the_parallel_workers() -> None:
    """`coverage run -m pytest` sees the controller only, not the xdist workers."""
    run = str(_by_name("ci.yml", "quality")["Tests with coverage"]["run"])

    assert "-n auto" in run
    assert "--cov" in run
    assert "coverage run" not in run


def test_weekly_sweep_runs_only_findings_that_appear_without_a_change() -> None:
    """A scheduled scan earns its runner only where the answer can move on its own.

    Advisories are published against a lockfile nobody touched, so they need a
    clock. Bandit reads source at a pinned version: between pull requests its
    answer cannot change, and every change carries a pull request that asks it.
    """
    triggers = _workflow("security.yml")["on"]
    assert isinstance(triggers, dict)
    names = set(_by_name("security.yml", "audit"))

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "17 3 * * 1"}]
    assert names == {"Sync", "Dependency audit", "UI dependency audit", "Worker dependency audit"}


def test_weekly_sweep_asks_every_ecosystem_even_after_a_finding() -> None:
    steps = _steps("security.yml", "audit")
    by_name = _by_name("security.yml", "audit")
    setup_node = next(
        step for step in steps if str(step.get("uses", "")).startswith("actions/setup-node@")
    )
    order = [str(step.get("name", step.get("uses", ""))) for step in steps]

    assert setup_node["if"] == "${{ !cancelled() }}"
    for ecosystem in ("UI dependency audit", "Worker dependency audit"):
        assert by_name[ecosystem]["if"] == "${{ !cancelled() }}"
    assert order.index("Dependency audit") < order.index("UI dependency audit")


def test_a_lockfile_edit_reaches_an_audit_without_waiting_for_the_sweep() -> None:
    """web.yml gates the Node lockfiles, so a change to one is audited before merge."""
    triggers = _workflow("web.yml")["on"]
    assert isinstance(triggers, dict)
    pull_request = triggers["pull_request"]
    assert isinstance(pull_request, dict)
    patterns = pull_request["paths"]
    assert isinstance(patterns, list)
    audited = _by_name("web.yml", "quality")

    for lockfile in ("ui/package-lock.json", "cloud/worker/package-lock.json"):
        assert any(PurePosixPath(lockfile).full_match(str(pattern)) for pattern in patterns)
    audit_command = "npm audit --package-lock-only --audit-level=high"
    assert audited["UI dependency audit"]["run"] == audit_command
    assert audited["Worker dependency audit"]["run"] == audit_command
