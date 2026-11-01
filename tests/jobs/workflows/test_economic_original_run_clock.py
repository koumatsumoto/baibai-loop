from pathlib import Path

import pytest
import yaml

from baibai_jobs.integration.economic_handoff import main


@pytest.mark.parametrize(
    ("created", "scheduled", "asof", "expected"),
    [
        ("2026-10-26T15:29:01+00:00", True, None, "2026-10-26"),
        ("2026-10-26T15:29:01+00:00", False, None, "2026-10-27"),
        ("2026-10-26T15:29:01+00:00", True, "2026-10-24", "2026-10-24"),
        ("2027-01-01T00:30:00+00:00", True, None, "2026-12-31"),
        ("2027-01-01T00:30:00+00:00", False, None, "2027-01-01"),
    ],
)
def test_economic_target_uses_original_schedule_slot_or_manual_date(
    created, scheduled, asof, expected, tmp_path, monkeypatch
):
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    args = ["plan", "--group", "fred_csv", "--run-created-at", created]
    if scheduled:
        args.append("--scheduled")
    if asof:
        args.extend(["--asof", asof])
    assert main(args) == 0
    assert main(args) == 0  # Rerun retains the original creation clock.
    targets = [line for line in output.read_text().splitlines() if line.startswith("asof=")]
    assert targets == [f"asof={expected}"] * 2


def test_economic_workflow_passes_schedule_mode_only_for_scheduled_runs():
    path = Path(__file__).resolve().parents[3] / ".github/workflows/cloud-economic-sources.yml"
    workflow = yaml.safe_load(path.read_text())
    plan = next(step for step in workflow["jobs"]["plan"]["steps"] if step.get("id") == "plan")
    assert 'if [ "$GITHUB_EVENT_NAME" = schedule ]; then args+=(--scheduled); fi' in plan["run"]
