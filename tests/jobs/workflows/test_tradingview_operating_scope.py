"""Daily acquisition uses only its existing runtime; receipt maintenance remains available."""

from pathlib import Path

import pytest
import yaml

from baibai_jobs.cli import build_parser

ROOT = Path(__file__).resolve().parents[3]


def test_daily_tradingview_uses_only_its_existing_runtime() -> None:
    target = ROOT / ".github/workflows/cloud-tradingview-snapshot.yml"
    workflow = yaml.load(target.read_text(), Loader=yaml.BaseLoader)
    assert workflow["jobs"]["snapshot"]["environment"] == "tradingview-runtime"
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        assert "tradingview_oauth_smoke" not in workflow["jobs"]
        if path != target:
            assert "TRADINGVIEW_OAUTH_STATE" not in path.read_text()
            assert "TRADINGVIEW_SECRET_WRITER_TOKEN" not in path.read_text()


def test_local_bulk_entry_is_removed_but_offline_rebuild_remains() -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args(["tradingview"])
    assert error.value.code == 2
    assert (
        build_parser().parse_args(["tradingview-source", "rebuild"]).command == "tradingview-source"
    )
