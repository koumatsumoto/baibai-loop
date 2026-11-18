"""The restored compact market machine must be fully hydrated before local reads."""

from __future__ import annotations

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
