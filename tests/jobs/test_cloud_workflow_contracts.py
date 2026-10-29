from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github/workflows"


def _workflow(name: str) -> dict[str, object]:
    loaded = yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _steps(workflow: dict[str, object], job: str) -> list[dict[str, object]]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    selected = jobs[job]
    assert isinstance(selected, dict)
    steps = selected["steps"]
    assert isinstance(steps, list)
    return steps


@pytest.mark.parametrize(
    "workflow_name",
    ["cloud-daily-batch.yml"],
)
def test_cloud_writers_share_one_non_cancelling_fifo_queue(workflow_name: str) -> None:
    workflow = _workflow(workflow_name)
    assert workflow["concurrency"]["group"] == "common-source-integration"
    concurrency = workflow["jobs"]["daily"]["concurrency"]

    assert concurrency == {
        "group": "cloud-publish",
        "queue": "max",
        "cancel-in-progress": "false",
    }


@pytest.mark.parametrize(
    ("workflow_name", "job", "first_read", "last_write", "purpose"),
    [
        ("cloud-daily-batch.yml", "daily", "pull", "portfolio-views", "daily"),
        (
            "cloud-materialize.yml",
            "materialize",
            "Pull stores",
            "Upload serving objects",
            "materialize",
        ),
    ],
)
def test_publication_lease_surrounds_every_cloud_writer(
    workflow_name: str, job: str, first_read: str, last_write: str, purpose: str
) -> None:
    workflow = _workflow(workflow_name)
    selected = workflow["jobs"][job]
    assert int(selected["timeout-minutes"]) < 120
    steps = _steps(workflow, job)
    names = [step.get("id", step.get("name")) for step in steps]
    assert names.index("lease-acquire") < names.index(first_read)
    assert names.index(last_write) < names.index("lease-release")
    acquire = steps[names.index("lease-acquire")]
    release = steps[names.index("lease-release")]
    assert f"--purpose {purpose}" in acquire["run"]
    assert '--handle "$RUNNER_TEMP/publication-lease-handle.json"' in acquire["run"]
    assert "acquired == 'true'" in release["if"]
    assert "always()" in release["if"]
    assert "continue-on-error" not in release
    for step in (acquire, release):
        assert {"R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"} <= set(step["env"])


def test_legacy_independent_deploy_workflow_is_removed() -> None:
    assert not (WORKFLOWS / "cloud-deploy.yml").exists()


def test_every_setup_uv_step_resolves_one_exact_root_version() -> None:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    required = config["tool"]["uv"]["required-version"]
    setup_steps: list[dict[str, object]] = []
    for path in WORKFLOWS.glob("*.yml"):
        workflow = _workflow(path.name)
        jobs = workflow["jobs"]
        assert isinstance(jobs, dict)
        for job in jobs.values():
            assert isinstance(job, dict)
            for step in job.get("steps", []):
                if str(step.get("uses", "")).startswith("astral-sh/setup-uv@"):
                    setup_steps.append(step)

    assert re.fullmatch(r"==\d+\.\d+\.\d+", required)
    assert setup_steps
    assert all("version" not in step.get("with", {}) for step in setup_steps)


def test_retained_web_reads_only_portfolio_sources_after_core_persistence():
    steps = _steps(_workflow("cloud-materialize.yml"), "materialize")
    commands = "\n".join(str(step.get("run", "")) for step in steps)
    assert "pull-market" in commands
    assert "pull-app" in commands
    assert "hydrate-market" in commands
    assert "pull-machine" not in commands
    assert "pull-runs" not in commands
    assert "upload-portfolio-views" in commands
    assert "publish-serving-tail" not in commands
    daily = _steps(_workflow("cloud-daily-batch.yml"), "daily")
    by_id = {step.get("id"): step for step in daily}
    assert "upload-stores.outputs.machine == 'success'" in by_id["portfolio-views"]["if"]
    assert daily.index(by_id["portfolio-views"]) > daily.index(by_id["source-seal"])
    assert "baibai_web" not in by_id["batch"]["run"]
    assert "baibai_web" not in by_id["source-seal"]["run"]
