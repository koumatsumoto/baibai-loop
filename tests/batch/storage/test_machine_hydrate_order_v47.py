"""The restored compact market machine must be fully hydrated before local reads."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    ("workflow", "job", "pull", "first_reader"),
    [
        ("cloud-daily-batch.yml", "daily", "pull-machine", "baibai-batch daily"),
        ("cloud-materialize.yml", "materialize", "pull-machine", "baibai_web.materialize"),
        (
            "cloud-external-assessment.yml",
            "publish",
            "pull-market",
            "external_assessment_sources publish",
        ),
        (
            "cloud-tradingview-snapshot.yml",
            "snapshot",
            "pull-market",
            "preflight_snapshot",
        ),
    ],
)
def test_machine_pull_and_full_hydrate_precede_first_market_reader(
    workflow: str, job: str, pull: str, first_reader: str
) -> None:
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    steps = document["jobs"][job]["steps"]
    commands = [str(step.get("run", "")) for step in steps]
    pull_steps = [i for i, command in enumerate(commands) if f"r2_transfer.sh {pull}" in command]
    hydrate_steps = [
        i for i, command in enumerate(commands) if "r2_transfer.sh hydrate-market" in command
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
