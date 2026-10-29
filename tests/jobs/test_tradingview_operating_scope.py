"""The repository batch has no TradingView live acquisition entry or credential use."""

from pathlib import Path

import pytest
import yaml

from baibai_jobs.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]


def test_no_cloud_tradingview_acquisition_or_oauth_smoke() -> None:
    assert not (ROOT / ".github/workflows/cloud-tradingview-snapshot.yml").exists()
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        assert "tradingview_oauth_smoke" not in workflow["jobs"]
        assert "TRADINGVIEW_OAUTH_STATE" not in path.read_text()
        assert "TRADINGVIEW_SECRET_WRITER_TOKEN" not in path.read_text()


def test_local_bulk_entry_is_removed_but_offline_rebuild_remains() -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["tradingview"])
    assert error.value.code == 2
    assert build_parser().parse_args(["tradingview-source", "rebuild"]).command == "tradingview-source"
