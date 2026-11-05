"""Restore the exact pre-cutover UTC slots recorded by PR #1826."""

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
def test_resumed_writer_has_original_active_slots_and_manual_entry(name):
    raw = (ROOT / ".github/workflows" / f"{name}.yml").read_text()
    assert declared_schedule(raw) == [{"cron": cron} for cron in SCHEDULES[name]]
    assert "workflow_dispatch" in yaml.load(raw, Loader=yaml.BaseLoader)["on"]
    assert "#1820 本番切替中は定期起動を停止" not in raw


@pytest.mark.parametrize(
    "raw",
    [
        "on:\n  workflow_dispatch: {}\n",
        'on:\n  # schedule:\n  #   - cron: "43 13 * * *"\n  workflow_dispatch: {}\n',
    ],
)
def test_comment_only_schedule_cannot_satisfy_resumed_trigger_contract(raw):
    with pytest.raises(KeyError):
        declared_schedule(raw)
