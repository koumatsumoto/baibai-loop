"""Market capture stays active; individual TradingView has no live bulk writer."""

from pathlib import Path

import pytest
import yaml
from tests.helpers.workflow_schedule import declared_schedule

from baibai_jobs.acquisition.tradingview.cli import main as archived_tradingview
from baibai_jobs.cli import build_parser

ROOT = Path(__file__).resolve().parents[3]


def test_only_market_tradingview_uses_its_existing_runtime() -> None:
    target = ROOT / ".github/workflows/cloud-tradingview-snapshot.yml"
    market = ROOT / ".github/workflows/cloud-tradingview-market-daily.yml"
    assert not target.exists()
    market_job = yaml.load(market.read_text(), Loader=yaml.BaseLoader)["jobs"]["capture"]
    assert market_job["environment"] == "tradingview-runtime"
    assert market_job["concurrency"] == {
        "group": "source-acquire-tradingview",
        "queue": "max",
        "cancel-in-progress": "false",
    }
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        assert "tradingview_oauth_smoke" not in workflow["jobs"]
        if path != market:
            assert "TRADINGVIEW_OAUTH_STATE" not in path.read_text()
            assert "TRADINGVIEW_SECRET_WRITER_TOKEN" not in path.read_text()


def test_local_bulk_entry_is_removed_but_offline_rebuild_remains() -> None:
    for command in ("tradingview", "master-dependency"):
        with pytest.raises(SystemExit) as error:
            build_parser().parse_args([command])
        assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        archived_tradingview(["refresh"])
    assert error.value.code == 2
    assert (
        build_parser().parse_args(["tradingview-source", "rebuild"]).command == "tradingview-source"
    )


def test_market_daily_is_a_separate_private_receipt() -> None:
    path = ROOT / ".github/workflows/cloud-tradingview-market-daily.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert [s["cron"] for s in declared_schedule(path.read_text())] == ["30 21 * * *"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["capture"]["timeout-minutes"] == "15"
    steps = workflow["jobs"]["capture"]["steps"]
    assert not any("upload-artifact" in step.get("uses", "") for step in steps)
    stage = next(
        step for step in steps if step.get("name") == "Save evidence in private source handoff"
    )
    assert "market-evidence stage" in stage["run"]
    capture = next(step for step in steps if step.get("id") == "capture")
    assert "cooldown-check.outputs.allowed == 'true'" in capture["if"]
    assert "timeout --signal=TERM --kill-after=30s 5m uv run" in capture["run"]
    assert any("tradingview.cooldown record" in step.get("run", "") for step in steps)
