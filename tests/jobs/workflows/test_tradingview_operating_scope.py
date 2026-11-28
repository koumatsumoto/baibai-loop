"""TradingView has no repository-operated live collection workflow."""

from pathlib import Path

import pytest
import yaml

from baibai_jobs.acquisition.tradingview.cli import main as archived_tradingview
from baibai_jobs.cli import build_parser

ROOT = Path(__file__).resolve().parents[3]


def test_market_capture_workflow_is_retired() -> None:
    directory = ROOT / ".github/workflows"
    assert not (directory / "cloud-tradingview-snapshot.yml").exists()
    name = "cloud-tradingview-market-daily.yml"
    retired = yaml.load((directory / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert set(retired["on"]) == {"workflow_call"}
    assert retired["permissions"] == {}
    assert set(retired["jobs"]) == {"retired"}
    assert retired["jobs"]["retired"]["if"] == "${{ false }}"
    for path in directory.glob("*.yml"):
        workflow = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        assert "tradingview_oauth_smoke" not in workflow["jobs"]
        assert "TRADINGVIEW_OAUTH_STATE" not in str(workflow)
        assert "TRADINGVIEW_SECRET_WRITER_TOKEN" not in str(workflow)
        assert all(
            job.get("uses") != f"./.github/workflows/{name}" for job in workflow["jobs"].values()
        )


def test_local_bulk_and_replay_commands_are_removed() -> None:
    for command in ("tradingview", "master-dependency"):
        with pytest.raises(SystemExit) as error:
            build_parser().parse_args([command])
        assert error.value.code == 2
    for command in ("refresh", "rebuild"):
        with pytest.raises(SystemExit) as error:
            archived_tradingview([command])
        assert error.value.code == 2
    assert (
        build_parser().parse_args(["tradingview-source", "receive"]).command == "tradingview-source"
    )
