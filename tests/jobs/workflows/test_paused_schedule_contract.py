from pathlib import Path

import pytest
import yaml
from tests.helpers.workflow_schedule import PAUSE, declared_schedule

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "name",
    [
        "cloud-article-sources",
        "cloud-daily-batch",
        "cloud-economic-sources",
        "cloud-external-assessment",
        "cloud-market-sources",
        "cloud-tradingview-market-daily",
        "cloud-tradingview-snapshot",
    ],
)
def test_paused_writer_keeps_original_slots_and_manual_recovery(name):
    raw = (ROOT / ".github/workflows" / f"{name}.yml").read_text()
    planned = declared_schedule(raw)
    assert all(set(item) == {"cron"} and len(item["cron"].split()) == 5 for item in planned)
    if PAUSE in raw.splitlines():
        on = yaml.load(raw, Loader=yaml.BaseLoader)["on"]
        assert "schedule" not in on
        assert "workflow_dispatch" in on


def test_marker_cannot_hide_an_active_schedule():
    raw = "on:\n" + PAUSE + '\n  schedule:\n    - cron: "43 13 * * *"\n  workflow_dispatch: {}\n'
    with pytest.raises(AssertionError, match="must not enable cron"):
        declared_schedule(raw)


def test_missing_schedule_requires_explicit_pause_and_saved_slots():
    with pytest.raises(KeyError):
        declared_schedule("on:\n  workflow_dispatch: {}\n")
    with pytest.raises(ValueError, match="not in list"):
        declared_schedule("on:\n" + PAUSE + "\n  workflow_dispatch: {}\n")


def test_paused_and_restored_schedule_have_identical_contract():
    raw = (
        'on:\n  schedule:\n    # planned slot\n    - cron: "43 13 * * *"\n  workflow_dispatch: {}\n'
    )
    paused = raw.replace(
        '  schedule:\n    # planned slot\n    - cron: "43 13 * * *"',
        PAUSE + '\n  # schedule:\n  #   # planned slot\n  #   - cron: "43 13 * * *"',
    )
    assert declared_schedule(raw) == declared_schedule(paused) == [{"cron": "43 13 * * *"}]
