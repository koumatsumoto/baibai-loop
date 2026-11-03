"""Daily acquisition uses only its existing runtime; receipt maintenance remains available."""

from pathlib import Path

import pytest
import yaml

from baibai_jobs.cli import build_parser

ROOT = Path(__file__).resolve().parents[3]


def test_daily_tradingview_uses_only_its_existing_runtime() -> None:
    target = ROOT / ".github/workflows/cloud-tradingview-snapshot.yml"
    market = ROOT / ".github/workflows/cloud-tradingview-market-daily.yml"
    workflow = yaml.load(target.read_text(), Loader=yaml.BaseLoader)
    assert workflow["jobs"]["snapshot"]["environment"] == "tradingview-runtime"
    snapshot_lock = workflow["jobs"]["snapshot"]["concurrency"]
    market_job = yaml.load(market.read_text(), Loader=yaml.BaseLoader)["jobs"]["capture"]
    assert market_job["environment"] == "tradingview-runtime"
    assert market_job["concurrency"] == snapshot_lock
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        assert "tradingview_oauth_smoke" not in workflow["jobs"]
        if path not in {target, market}:
            assert "TRADINGVIEW_OAUTH_STATE" not in path.read_text()
            assert "TRADINGVIEW_SECRET_WRITER_TOKEN" not in path.read_text()


def test_local_bulk_entry_is_removed_but_offline_rebuild_remains() -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["tradingview"])
    assert error.value.code == 2
    assert (
        build_parser().parse_args(["tradingview-source", "rebuild"]).command == "tradingview-source"
    )


def test_market_daily_is_a_separate_private_receipt_without_reducing_snapshots() -> None:
    path = ROOT / ".github/workflows/cloud-tradingview-market-daily.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert [s["cron"] for s in workflow["on"]["schedule"]] == ["30 21 * * *"]
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["capture"]["steps"]
    assert not any("upload-artifact" in step.get("uses", "") for step in steps)
    stage = next(
        step for step in steps if step.get("name") == "Save evidence in private source handoff"
    )
    assert "market-evidence stage" in stage["run"]
    capture = next(step for step in steps if step.get("id") == "capture")
    assert "cooldown-check.outputs.allowed == 'true'" in capture["if"]
    assert any("tradingview.cooldown record" in step.get("run", "") for step in steps)
