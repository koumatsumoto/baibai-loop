from __future__ import annotations

import os
import re
import subprocess
import tomllib
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from baibai_jobs.publication.lease import DAILY_LEASE_TTL, LEASE_TTL

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
    ["cloud-daily-batch-current.yml"],
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
        ("cloud-daily-batch-current.yml", "daily", "pull", "portfolio-views", "daily"),
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
    ttl = DAILY_LEASE_TTL if purpose == "daily" else LEASE_TTL
    assert timedelta(minutes=int(selected["timeout-minutes"])) <= ttl - timedelta(minutes=10)
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


def test_worker_deploy_token_is_limited_to_verified_main_push_or_dispatch() -> None:
    """Check the declared Wrangler entry point and gate without executing a deployment."""
    grants = []
    for path in WORKFLOWS.glob("*.y*ml"):
        workflow = _workflow(path.name)
        for job_id, job in workflow["jobs"].items():
            for scope in (workflow, job, *job.get("steps", [])):
                if "CLOUDFLARE_API_TOKEN" in scope.get("env", {}):
                    assert scope.get("working-directory") == "web/edge"
                    command = scope.get("run", "").strip(" \t\n")
                    assert re.fullmatch(r"npx[ \t]+wrangler[ \t]+deploy", command)
                    guard = scope["if"].removeprefix("${{").removesuffix("}}")
                    grants.append((path.name, job_id, {term.strip() for term in guard.split("&&")}))
    assert grants == [
        (
            "web.yml",
            "quality",
            {
                "success()",
                "env.PRODUCTION == 'true'",
                "steps.deploy-target.outputs.current == 'true'",
            },
        )
    ]

    web = _workflow("web.yml")
    assert web["env"]["PRODUCTION"] == (
        "${{ github.ref == 'refs/heads/main' && "
        "(github.event_name == 'push' || github.event_name == 'workflow_dispatch') }}"
    )
    job = web["jobs"]["quality"]
    steps = _steps(web, "quality")
    assert all("PRODUCTION" not in item.get("env", {}) for item in (job, *steps))
    verify = next(step for step in steps if step.get("id") == "deploy-target")
    assert verify["if"] == "${{ env.PRODUCTION == 'true' }}"
    assert steps.index(verify) < next(
        i for i, step in enumerate(steps) if "CLOUDFLARE_API_TOKEN" in step.get("env", {})
    )


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
    assert "pull-portfolio-market" in commands
    assert "pull-app" in commands
    assert "r2_transfer.sh pull-market" not in commands
    assert "hydrate-portfolio" not in commands
    assert "hydrate-market" not in commands
    assert "pull-machine" not in commands
    assert "pull-runs" not in commands
    assert "upload-portfolio-views" in commands
    assert "publish-serving-tail" not in commands
    daily = _steps(_workflow("cloud-daily-batch-current.yml"), "daily")
    by_id = {step.get("id"): step for step in daily}
    assert by_id["portfolio-views"]["if"] == (
        "${{ always() && steps.batch.outputs.models_ready == 'true' "
        "&& steps.batch.outcome == 'success' }}"
    )
    assert daily.index(by_id["portfolio-views"]) > daily.index(by_id["source-seal"])
    assert [step.get("id") for step in daily if "pull-app" in step.get("run", "")] == [
        "portfolio-views"
    ]
    portfolio_run = by_id["portfolio-views"]["run"]
    assert portfolio_run.index("pull-app") < portfolio_run.index("baibai_web.materialize")
    assert portfolio_run.index("baibai_web.materialize") < portfolio_run.index(
        "upload-portfolio-views"
    )
    assert "pull-machine" in by_id["pull"]["run"]
    assert "baibai_web" not in by_id["batch"]["run"]
    assert "baibai_web" not in by_id["source-seal"]["run"]


def test_shared_transfer_callers_pass_the_acquired_publication_handle():
    daily = _steps(_workflow("cloud-daily-batch-current.yml"), "daily")
    materialize = _steps(_workflow("cloud-materialize.yml"), "materialize")
    callers = [
        step
        for step in [*daily, *materialize]
        if "r2_transfer.sh pull-machine --daily-preflight" in step.get("run", "")
        or "r2_transfer.sh hydrate-market-for-daily" in step.get("run", "")
        or "r2_transfer.sh upload-portfolio-views" in step.get("run", "")
    ]
    assert len(callers) == 4
    for step in callers:
        assert step["env"]["PUBLICATION_LEASE_HANDLE"] == (
            "${{ runner.temp }}/publication-lease-handle.json"
        )


def test_materialize_keeps_verified_application_independent_of_market_failure():
    steps = _steps(_workflow("cloud-materialize.yml"), "materialize")
    by_id = {step.get("id"): step for step in steps}
    app, market, pin, materialize = (
        by_id[name] for name in ("pull-app", "market", "listing-master", "materialize")
    )
    upload = next(step for step in steps if step.get("name") == "Upload serving objects")
    ordered = [steps.index(step) for step in (app, market, pin, materialize, upload)]
    assert ordered == sorted(ordered)
    assert pin["if"] == "${{ steps.market.outcome == 'success' }}"
    assert "continue-on-error" not in pin
    assert (
        sum(
            "baibai_jobs.integration.listing_master_current --output" in step.get("run", "")
            for step in steps
        )
        == 1
    )
    assert '--output "$RUNNER_TEMP/current-listing-master.json"' in pin["run"]
    assert "jobs/scripts/r2_transfer.sh pull-app" in app["run"]
    assert market["run"].splitlines() == ["jobs/scripts/r2_transfer.sh pull-portfolio-market"]
    assert "continue-on-error" not in market
    assert materialize["if"] == (
        "${{ !cancelled() && steps.lease-acquire.outputs.acquired == 'true' "
        "&& steps.pull-app.outcome == 'success' }}"
    )
    assert materialize["env"]["MARKET_OUTCOME"] == "${{ steps.market.outcome }}"
    assert materialize["env"]["LISTING_MASTER_OUTCOME"] == "${{ steps.listing-master.outcome }}"
    assert "R2_SECRET_ACCESS_KEY" not in materialize["env"]
    current_revision = by_id["current-revision"]
    assert steps.index(materialize) < steps.index(current_revision) < steps.index(upload)
    assert current_revision["if"] == "${{ !cancelled() && steps.materialize.outcome == 'success' }}"
    assert upload["if"] == "${{ !cancelled() && steps.current-revision.outcome == 'success' }}"


@pytest.mark.parametrize(("main_is_current", "api_exit"), [(True, 0), (False, 0), (True, 1)])
def test_materialize_checks_main_before_upload(tmp_path, main_is_current, api_exit):
    steps = _steps(_workflow("cloud-materialize.yml"), "materialize")
    script = next(step["run"] for step in steps if step.get("id") == "current-revision")
    checked_out = "a" * 40
    current = checked_out if main_is_current else "b" * 40
    # Exercise the actual workflow shell, not a second implementation.
    stubs = f"""
git() {{ [[ "$*" == 'rev-parse HEAD' ]] || return 99; echo {checked_out}; }}
gh() {{
  [[ "$*" == "api --method GET repos/koumatsumoto/baibai-loop/git/ref/heads/main --jq .object.sha" ]] || return 99
  echo {current}
  return {api_exit}
}}
"""
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", stubs + script],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_REPOSITORY": "koumatsumoto/baibai-loop"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == (0 if main_is_current and not api_exit else 1)
    if not main_is_current:
        assert "regenerate views from the current main" in result.stderr


@pytest.mark.parametrize(
    ("market_outcome", "market_present", "listing_outcome"),
    [
        ("success", True, "success"),
        ("success", True, "failure"),
        ("failure", True, "skipped"),
        ("failure", False, "skipped"),
    ],
)
def test_materialize_excludes_unverified_market_copies_but_keeps_application(
    tmp_path, market_outcome, market_present, listing_outcome
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
    (tmp_path / "current-listing-master.json").write_bytes(b"would be unsafe after a failed pin")
    uv.write_text(
        """#!/bin/sh
set -eu
if test -f stores/market/market.sqlite; then echo market-present; else echo market-absent; fi
until test "$1" = --current-listing-master; do shift; done
shift
if test "$LISTING_MASTER_OUTCOME" = success; then
    test "$1" = "$RUNNER_TEMP/current-listing-master.json"
    test -f "$1"
    echo listing-pinned
else
    test ! -e "$1"
    echo listing-unavailable
fi
"""
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
            "LISTING_MASTER_OUTCOME": listing_outcome,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    verified = market_outcome == "success"
    assert result.stdout == ""
    assert (tmp_path / "materialize-private.log").read_text().splitlines() == [
        "market-present" if verified else "market-absent",
        "listing-pinned" if listing_outcome == "success" else "listing-unavailable",
    ]
    assert app.read_bytes() == b"verified application"
    if market_present:
        retained = market if verified else tmp_path / "unverified-market/market.sqlite"
        assert retained.read_bytes() == b"downloaded market"


def test_standalone_materialize_job_runs_only_on_main() -> None:
    workflow = _workflow("cloud-materialize.yml")
    assert workflow["jobs"]["materialize"]["if"] == "${{ github.ref == 'refs/heads/main' }}"


@pytest.mark.parametrize("transfer_exit", [0, 31])
def test_manual_display_uses_only_current_and_preserves_transfer_outcome(tmp_path, transfer_exit):
    workflow = _workflow("cloud-materialize.yml")
    assert set(workflow["on"]) == {"workflow_dispatch", "workflow_call"}
    assert not workflow["on"]["workflow_call"]
    assert not workflow["on"]["workflow_dispatch"]
    steps = _steps(workflow, "materialize")
    assert "machine_bundle" not in str(steps)
    by_id = {step.get("id", step.get("name")): step for step in steps}
    assert steps.index(by_id["lease-acquire"]) < steps.index(by_id["market"])
    scripts = tmp_path / "jobs/scripts"
    scripts.mkdir(parents=True)
    transfer = scripts / "r2_transfer.sh"
    transfer.write_text(f'#!/bin/sh\nprintf "%s\n" "$@" >> calls\nexit {transfer_exit}\n')
    transfer.chmod(0o755)
    for id_, expected in (
        ("market", ["pull-portfolio-market"]),
        ("Upload serving objects", ["upload-portfolio-views", str(tmp_path / "serving")]),
    ):
        result = subprocess.run(
            ["bash", "-euo", "pipefail", "-c", by_id[id_]["run"]],
            cwd=tmp_path,
            env={**os.environ, "RUNNER_TEMP": str(tmp_path)},
            check=False,
        )
        assert result.returncode == transfer_exit
        calls = tmp_path / "calls"
        assert calls.read_text().splitlines() == expected
        calls.unlink()
