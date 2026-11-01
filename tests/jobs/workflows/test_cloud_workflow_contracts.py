from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
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
            "pull-app",
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
    assert [step.get("id") for step in daily if "pull-app" in step.get("run", "")] == [
        "portfolio-views"
    ]
    assert by_id["portfolio-views"]["run"].splitlines()[0] == "jobs/scripts/r2_transfer.sh pull-app"
    assert "pull-machine" in by_id["pull"]["run"]
    assert "baibai_web" not in by_id["batch"]["run"]
    assert "baibai_web" not in by_id["source-seal"]["run"]


def test_materialize_keeps_verified_application_independent_of_market_failure():
    steps = _steps(_workflow("cloud-materialize.yml"), "materialize")
    by_id = {step.get("id"): step for step in steps}
    app, market, materialize = (by_id[name] for name in ("pull-app", "market", "materialize"))
    upload = next(step for step in steps if step.get("name") == "Upload serving objects")
    assert steps.index(app) < steps.index(market) < steps.index(materialize) < steps.index(upload)
    assert app["run"] == "jobs/scripts/r2_transfer.sh pull-app"
    assert market["run"].splitlines() == [
        "jobs/scripts/r2_transfer.sh pull-market",
        "jobs/scripts/r2_transfer.sh hydrate-market",
    ]
    assert "continue-on-error" not in market
    assert materialize["if"] == "${{ !cancelled() && steps.pull-app.outcome == 'success' }}"
    assert materialize["env"]["MARKET_OUTCOME"] == "${{ steps.market.outcome }}"
    assert upload["if"] == "${{ !cancelled() && steps.materialize.outcome == 'success' }}"


@pytest.mark.parametrize(
    ("market_outcome", "market_present"),
    [("success", True), ("failure", True), ("failure", False)],
)
def test_materialize_excludes_unverified_market_copies_but_keeps_application(
    tmp_path, market_outcome, market_present
):
    steps = _steps(_workflow("cloud-materialize.yml"), "materialize")
    materialize = next(step for step in steps if step.get("id") == "materialize")
    app = tmp_path / "stores/application/baibai.sqlite"
    app.parent.mkdir(parents=True)
    app.write_bytes(b"verified application")
    market = tmp_path / "stores/market/market.sqlite"
    if market_present:
        market.parent.mkdir(parents=True)
        market.write_bytes(b"downloaded market")
    binary = tmp_path / "bin"
    binary.mkdir()
    uv = binary / "uv"
    uv.write_text(
        "#!/bin/sh\nif test -f stores/market/market.sqlite; then echo present; else echo absent; fi\n"
    )
    uv.chmod(0o755)

    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", materialize["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "MARKET_OUTCOME": market_outcome,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    verified = market_outcome == "success"
    assert result.stdout.strip() == ("present" if verified else "absent")
    assert app.read_bytes() == b"verified application"
    if market_present:
        retained = market if verified else tmp_path / "unverified-market/market.sqlite"
        assert retained.read_bytes() == b"downloaded market"
