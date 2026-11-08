"""JPX bootstrap keeps the selected source isolated from retired J-Quants work."""

import io
import sqlite3
from datetime import date
from unittest.mock import Mock

import pytest
from tests.helpers.market_maintenance import FakeJPXProvider

from baibai_engine.machine_analysis.observations.disclosures.jpx_models import JPXProviderError
from baibai_jobs.maintenance.market.cache import bootstrap_cache_command
from baibai_jobs.maintenance.market.providers import ProviderBundle

DAY = date(2026, 5, 8)


@pytest.mark.parametrize("group", ["jpx-earnings", "jpx-regulations"])
def test_jpx_bootstrap_acquires_only_the_selected_group(tmp_path, group):
    jpx = FakeJPXProvider()
    output = io.StringIO()
    assert (
        bootstrap_cache_command(
            asof_date=DAY,
            providers=ProviderBundle(jpx_refresh=jpx),
            sqlite_path=tmp_path / "market.sqlite",
            source_group=group,
            stdout=output,
        )
        == 0
    )
    assert jpx.earnings_requests == ([DAY] if group == "jpx-earnings" else [])
    assert jpx.regulation_requests == ([DAY] if group == "jpx-regulations" else [])
    assert "bootstrap-cache done" in output.getvalue()


@pytest.mark.parametrize("group", ["jpx-earnings", "jpx-regulations"])
@pytest.mark.parametrize(
    "error", [JPXProviderError("missing source"), sqlite3.DatabaseError("bad db")]
)
def test_jpx_bootstrap_reports_owner_failure(tmp_path, capsys, group, error):
    jpx = Mock()
    handler = (
        jpx.refresh_earnings_calendar_snapshot
        if group == "jpx-earnings"
        else jpx.refresh_regulation_snapshot
    )
    handler.side_effect = error
    assert (
        bootstrap_cache_command(
            asof_date=DAY,
            providers=ProviderBundle(jpx_refresh=jpx),
            sqlite_path=tmp_path / "market.sqlite",
            source_group=group,
        )
        == 1
    )
    output = capsys.readouterr()
    assert str(error) in output.err
    assert "bootstrap-cache incomplete" in output.out
    assert "bootstrap-cache done" not in output.out


@pytest.mark.parametrize("group", ["all", "jquants-market", "jquants-financial", "jpx", "unknown"])
def test_removed_bootstrap_groups_fail_before_touching_providers(tmp_path, group):
    database = tmp_path / "absent.sqlite"
    jpx = Mock()
    with pytest.raises(ValueError, match="unsupported acquisition source group"):
        bootstrap_cache_command(
            asof_date=DAY,
            providers=ProviderBundle(jpx_refresh=jpx),
            sqlite_path=database,
            source_group=group,
        )
    assert jpx.mock_calls == []
    assert not database.exists()
