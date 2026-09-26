from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"

_NPM_AUDIT = "npm audit --package-lock-only --audit-level=high"
_BANDIT = (
    "uv run bandit -c pyproject.toml -q -r engine/src/baibai_engine "
    "web/backend/src/baibai_web batch/src/baibai_batch tools"
)
# A scanner is weakened by editing its command, not by deleting its step: a lowered
# --audit-level, a narrowed Bandit root, an --ignore-vuln or a trailing `|| true`
# all leave a green step whose name still reads like the gate it used to be.
_SCANNER_COMMANDS = {
    ("ci.yml", "quality", "Bandit"): _BANDIT,
    ("node-audit.yml", "audit", "UI dependency audit"): _NPM_AUDIT,
    ("node-audit.yml", "audit", "Worker dependency audit"): _NPM_AUDIT,
    ("security.yml", "audit", "UI dependency audit"): _NPM_AUDIT,
    ("security.yml", "audit", "Worker dependency audit"): _NPM_AUDIT,
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
    """No gate re-run is billed for a tree that already passed.

    Actions bills each job by the minute, and while the base holds still the merge
    ref a pull request checks is the tree that lands on main — so a second run over
    identical bytes is paid for and proves nothing new.
    """
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

    assert set(jobs) == {"quality", "tradingview_oauth_smoke"}
    assert {
        "Ruff format",
        "Ruff lint",
        "Type check",
        "Import contracts",
        "Drift gates",
        "Tests with coverage",
        "Bandit",
        "Dependency audit",
    } <= names
    assert not names.intersection(_by_name("ci.yml", "tradingview_oauth_smoke"))


def test_ci_pins_the_worker_count_its_runner_measured_fastest() -> None:
    """`auto` resolves to the runner's 2 cores; the suite waits on sqlite, not the CPU.

    Measured on the runner, oversubscribing stops paying well before it stops being
    tried: 8 workers take 92s and 16 take 130s, while 2 and 4 both fall inside the
    74-95s the runner varies across. Developer machines keep `auto` from `addopts`,
    where the box is wider.
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


def test_the_python_dependency_audit_suppresses_nothing() -> None:
    run = str(_by_name("ci.yml", "quality")["Dependency audit"]["run"])

    assert "uv run pip-audit" in run
    assert "--all-groups" in run
    assert "--ignore-vuln" not in run
    assert "|| true" not in run


def test_scanners_are_asked_even_after_an_earlier_gate_fails() -> None:
    """They share ci's runner to avoid a second billed job, not to share its fate."""
    by_name = _by_name("ci.yml", "quality")

    for scanner in ("Bandit", "Dependency audit"):
        assert by_name[scanner]["if"] == "${{ !cancelled() }}"


@pytest.mark.parametrize("workflow_name", ["ci.yml", "web.yml", "security.yml", "node-audit.yml"])
def test_a_hung_job_cannot_bill_an_open_ended_amount(workflow_name: str) -> None:
    jobs = _workflow(workflow_name)["jobs"]
    assert isinstance(jobs, dict)

    for job in jobs.values():
        assert isinstance(job, dict)
        assert int(str(job["timeout-minutes"])) <= 20


def test_node_audit_is_triggered_by_the_lockfiles_it_reads() -> None:
    """Scoping by trigger keeps an advisory from blocking unrelated web work.

    A published advisory turns this red on any run, so the run has to happen only
    when a resolution actually changed. Advisories against a lockfile standing
    still are the weekly sweep's finding, not a merge gate's.
    """
    workflow = _workflow("node-audit.yml")
    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    pull_request = triggers["pull_request"]
    assert isinstance(pull_request, dict)
    patterns = pull_request["paths"]
    assert isinstance(patterns, list)

    lockfiles = ["web/frontend/package-lock.json", "web/edge/package-lock.json"]

    assert set(triggers) == {"pull_request"}
    assert set(jobs) == {"audit"}
    # Named exactly, not merely covered: `web/frontend/**` would also satisfy a containment
    # check while handing every UI change to a scanner that answers from outside.
    assert patterns == [*lockfiles, ".github/workflows/node-audit.yml"]
    for lockfile in lockfiles:
        assert any(PurePosixPath(lockfile).full_match(str(pattern)) for pattern in patterns)
    for ecosystem in ("UI dependency audit", "Worker dependency audit"):
        assert _by_name("node-audit.yml", "audit")[ecosystem]["if"] == "${{ !cancelled() }}"


def test_weekly_sweep_asks_only_what_a_pull_request_cannot() -> None:
    """A scheduled runner earns its minute only where the answer moves on its own.

    Advisories are published against a lockfile nobody touched. `check_workflow_trust`
    is asked again because main can advance past the merge ref a pull request checked,
    and main is the tree that runs with credentials. Bandit reads source at a pinned
    version, so between pull requests its answer cannot change at all.
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
        "UI dependency audit",
        "Worker dependency audit",
    }


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
