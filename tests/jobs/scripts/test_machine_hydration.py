"""The restored compact market machine must be fully hydrated before local reads."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from tests.helpers.r2_transfer import _environment, _fake_aws, _fake_repo

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "command", ["hydrate-market", "hydrate-market-for-daily", "hydrate-portfolio"]
)
def test_transfer_dispatches_the_requested_hydration_scope(tmp_path, command):
    root = _fake_repo(tmp_path)
    bin_dir, log = _fake_aws(tmp_path)
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", command],
        env=_environment(bin_dir, log),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    expected = {
        "hydrate-market": "hydrate",
        "hydrate-market-for-daily": "hydrate-for-daily",
        "hydrate-portfolio": "hydrate-portfolio",
    }[command]
    calls = log.read_text().splitlines()
    assert any(f"baibai-jobs snapshots {expected} --mirror " in line for line in calls)
    assert not any("s3api" in line or "s3 cp" in line for line in calls)


@pytest.mark.parametrize(
    ("workflow", "job", "pull", "first_reader"),
    [
        ("cloud-daily-batch.yml", "daily", "pull-machine", "integration.sources apply"),
    ],
)
def test_machine_pull_and_daily_hydrate_precede_first_market_reader(
    workflow: str, job: str, pull: str, first_reader: str
) -> None:
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    steps = document["jobs"][job]["steps"]
    commands = [str(step.get("run", "")) for step in steps]
    pull_steps = [i for i, command in enumerate(commands) if f"r2_transfer.sh {pull}" in command]
    hydrate_steps = [
        i
        for i, command in enumerate(commands)
        if "r2_transfer.sh hydrate-market-for-daily" in command
    ]
    reader_steps = [i for i, command in enumerate(commands) if first_reader in command]
    assert len(pull_steps) == len(hydrate_steps) == len(reader_steps) == 1
    pull_index, hydrate_index, reader_index = (
        pull_steps[0],
        hydrate_steps[0],
        reader_steps[0],
    )
    assert pull_index <= hydrate_index < reader_index
    if pull_index == hydrate_index:
        assert commands[pull_index].index(f"r2_transfer.sh {pull}") < commands[pull_index].index(
            "r2_transfer.sh hydrate-market"
        )


def test_source_acquisitions_never_pull_or_hydrate_the_full_machine():
    for workflow, job in (
        ("cloud-market-source.yml", "acquire"),
        ("cloud-economic-source.yml", "acquire"),
        ("cloud-external-assessment-source.yml", "acquire"),
    ):
        document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
        text = str(document["jobs"][job])
        for command in (
            "pull-market",
            "pull-machine",
            "pull-app",
            "hydrate-market",
            "publish-lake",
            "publication_lease",
        ):
            assert command not in text


@pytest.mark.parametrize("preflight_exit", [0, 41])
def test_daily_pull_gates_the_same_transfer_before_installing_both_copies(tmp_path, preflight_exit):
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    env = _environment(bin_dir, log)
    env["AWS_FAKE_EXISTING_KEY"] = "machine-manifest.json"
    (Path(env["AWS_FAKE_STATE"]) / "put-machine-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "keys": {"market.sqlite": "etag-stable", "runs.sqlite": "etag-stable"},
            }
        )
    )
    local = {
        "market.sqlite": root / "stores/market/market.sqlite",
        "runs.sqlite": root / "stores/screening/runs.sqlite",
    }
    for key, path in local.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"original {key}")
    uv = bin_dir / "uv"
    fallback = bin_dir / "uv-transfer-fixture"
    uv.rename(fallback)
    env["PREFLIGHT_FIXTURE_EXIT"] = str(preflight_exit)
    uv.write_text(
        r"""#!/bin/bash
set -euo pipefail
if [[ "${1} ${2} ${3} ${4}" == 'run python -m baibai_jobs.publication.machine_preflight' ]]; then
  [[ "$5" == --market && "$7" == --receipt && "$9" == --bucket ]]
  [[ "${11}" == --lease-handle && "${12}" == "$PUBLICATION_LEASE_HANDLE" ]]
  [[ $(dirname "$6") == $(dirname "$8") && $(cat "$6") == x ]]
  [[ $(cat "$(dirname "$6")/runs.sqlite") == x ]]
  grep -q etag-stable "$8"
  printf 'preflight\n' >> "$AWS_LOG"
  exit "$PREFLIGHT_FIXTURE_EXIT"
fi
exec "$(dirname "$0")/uv-transfer-fixture" "$@"
"""
    )
    uv.chmod(0o755)
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-machine", "--daily-preflight"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == preflight_exit, result.stderr
    calls = log.read_text().splitlines()
    assert calls.count("preflight") == 1
    assert sum(line.startswith("s3 cp s3://") for line in calls) == 2
    assert sum(line.startswith("s3api get-object ") for line in calls) == 1
    assert all(
        path.read_text() == (f"original {key}" if preflight_exit else "x")
        for key, path in local.items()
    )
    for key in local:
        assert (Path(env["R2_GENERATION_DIR"]) / f"{key}.etag").exists() is (preflight_exit == 0)
    assert not any(line.startswith(("s3api put-object", "s3api copy-object")) for line in calls)


def test_daily_pull_requires_a_handle_before_any_transfer(tmp_path):
    bin_dir, log = _fake_aws(tmp_path)
    root = _fake_repo(tmp_path)
    env = _environment(bin_dir, log)
    env.pop("PUBLICATION_LEASE_HANDLE")
    result = subprocess.run(
        [root / "jobs/scripts/r2_transfer.sh", "pull-machine", "--daily-preflight"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert not log.exists()
