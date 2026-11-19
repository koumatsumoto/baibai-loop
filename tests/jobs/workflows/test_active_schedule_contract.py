"""Operational writers keep their UTC slots and manual entry points."""

from pathlib import Path

import pytest
import yaml
from tests.helpers.workflow_schedule import declared_schedule

ROOT = Path(__file__).resolve().parents[3]
SCHEDULES = {
    "cloud-article-sources": ["13 1,5,9,13,17,21 * * *"],
    "cloud-daily-batch": ["43 13 * * *"],
    "cloud-economic-sources": ["43 7 * * *"],
    "cloud-external-assessment": ["13 10,14,22 * * *"],
    "cloud-market-sources": ["43 7 * * *", "43 8 * * *", "13 9 * * *"],
    "cloud-tradingview-market-daily": ["30 21 * * *"],
    "cloud-tradingview-snapshot": ["37 6,8,10,12,14 * * 1-5", "37 0,4,8 * * 0,6"],
}


@pytest.mark.parametrize("name", SCHEDULES)
def test_writer_has_active_slots_and_manual_entry(name):
    raw = (ROOT / ".github/workflows" / f"{name}.yml").read_text()
    assert declared_schedule(raw) == [{"cron": cron} for cron in SCHEDULES[name]]
    assert "workflow_dispatch" in yaml.load(raw, Loader=yaml.BaseLoader)["on"]


@pytest.mark.parametrize(
    "raw",
    [
        "on:\n  workflow_dispatch: {}\n",
        'on:\n  # schedule:\n  #   - cron: "43 13 * * *"\n  workflow_dispatch: {}\n',
    ],
)
def test_comment_only_schedule_is_not_an_active_trigger(raw):
    with pytest.raises(KeyError):
        declared_schedule(raw)
