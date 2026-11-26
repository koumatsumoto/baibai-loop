"""The shell hands one fixed Machine/Lake view to the Python owner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from tests.helpers.r2_transfer import _environment, _fake_aws, _fake_repo

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("command", "mode"),
    [
        (("pull-machine",), "full"),
        (("pull-machine", "--daily-preflight"), "daily"),
        (("pull-portfolio-market",), "portfolio"),
        (("hydrate-market-for-daily",), "hydrate-daily"),
    ],
)
@pytest.mark.parametrize("pull_exit", [0, 41])
def test_transfer_dispatches_one_pinned_hydration_path(tmp_path, command, mode, pull_exit):
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    env = _environment(bin_dir, log)
    mirror = tmp_path / "cache with spaces"
    env.update(
        R2_STORES_BUCKET="synthetic-selected-stores",
        R2_LAKE_MIRROR=str(mirror),
        MACHINE_TEST_EXIT=str(pull_exit),
    )
    uv = bin_dir / "uv"
    uv.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["AWS_LOG"], "a", encoding="utf-8") as log:
    print(json.dumps({"cwd": os.getcwd(), "args": sys.argv[1:]}), file=log)
if "baibai_jobs.publication.machine_pull" in sys.argv:
    sys.exit(int(os.environ["MACHINE_TEST_EXIT"]))
"""
    )
    uv.chmod(0o755)
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", *command],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == pull_exit, result.stderr
    layout, transfer = [json.loads(line) for line in log.read_text().splitlines()]
    assert layout == {
        "cwd": str(root),
        "args": ["run", "python", "-m", "baibai_jobs.observability.repository_layout"],
    }
    required = (
        ["--runs", str(root / "stores/screening/runs.sqlite")] if mode in {"full", "daily"} else []
    )
    if mode in {"daily", "hydrate-daily"}:
        required += ["--lease-handle", env["PUBLICATION_LEASE_HANDLE"]]
    assert transfer == {
        "cwd": str(root),
        "args": [
            "run",
            "python",
            "-m",
            "baibai_jobs.publication.machine_pull",
            *(["hydrate-daily"] if mode == "hydrate-daily" else ["pull", "--mode", mode]),
            "--market",
            str(root / "stores/market/market.sqlite"),
            "--mirror",
            str(mirror),
            "--bucket",
            env["R2_STORES_BUCKET"],
            *required,
        ],
    }


def test_daily_preflight_gates_economic_but_hydration_only_gates_market_consumers():
    document = yaml.safe_load(
        (ROOT / ".github/workflows/cloud-daily-batch-current.yml").read_text()
    )
    steps = document["jobs"]["daily"]["steps"]
    indexed = {step["id"]: (index, step) for index, step in enumerate(steps) if "id" in step}
    assert (
        indexed["lease-acquire"][0]
        < indexed["pull"][0]
        < indexed["economic-l1"][0]
        < indexed["hydrate"][0]
        < indexed["source-integration"][0]
    )
    assert indexed["pull"][1]["run"].strip() == (
        "jobs/scripts/r2_transfer.sh pull-machine --daily-preflight"
    )
    assert "PUBLICATION_LEASE_HANDLE" in indexed["pull"][1]["env"]
    assert (
        indexed["hydrate"][1]["run"].strip()
        == "jobs/scripts/r2_transfer.sh hydrate-market-for-daily"
    )
    assert "PUBLICATION_LEASE_HANDLE" in indexed["hydrate"][1]["env"]


def test_source_acquisitions_never_pull_or_hydrate_the_full_machine():
    for workflow, job in (
        ("cloud-market-source.yml", "acquire"),
        ("cloud-economic-source-current.yml", "acquire"),
        ("cloud-external-assessment-source.yml", "acquire"),
    ):
        document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
        text = str(document["jobs"][job])
        for command in (
            "pull-portfolio-market",
            "pull-machine",
            "hydrate-market-for-daily",
            "pull-app",
            "publication_lease",
        ):
            assert command not in text


@pytest.mark.parametrize(
    "command", [("pull-machine", "--daily-preflight"), ("hydrate-market-for-daily",)]
)
def test_daily_steps_require_a_handle_before_any_transfer(tmp_path, command):
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    env = _environment(bin_dir, log)
    env.pop("PUBLICATION_LEASE_HANDLE")
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", *command],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert not log.exists()
