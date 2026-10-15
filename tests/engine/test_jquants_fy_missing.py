"""FY provider reads must not turn missing latest earnings into older anchors."""

from datetime import date
from unittest.mock import patch

import pytest

from baibai_engine.market.providers.jquants import JQuantsProvider
from baibai_engine.storage.observations.ingest.jquants import store_jquants_fin_summaries
from baibai_engine.valuation.metrics.profit import build_normalized_profit_signals


@pytest.mark.parametrize("cached", [False, True])
def test_fy_provider_retains_latest_year_without_eps(tmp_path, cached):
    start, end = date(2021, 1, 1), date(2026, 6, 1)
    records = [
        {
            "Code": "13010",
            "DisclosedDate": f"{year}-05-15",
            "TypeOfCurrentPeriod": "FY",
            "CurrentFiscalYearEndDate": f"{year}-03-31",
            "EarningsPerShare": 10.0 if year < 2026 else None,
        }
        for year in range(2021, 2027)
    ]
    database = tmp_path / "market.sqlite"
    if cached:
        store_jquants_fin_summaries(database, records, requested_start=start, requested_end=end)
    provider = JQuantsProvider(
        "unused", sqlite_path=database if cached else None, cache_only=cached
    )
    with patch.object(provider, "_load_or_fetch_range", return_value=records) as fetch:
        rows = provider.get_fy_summary_range(start, end)
    assert fetch.call_count == (0 if cached else 1)
    assert len(rows) == 6
    assert rows[-1].fiscal_year_end == date(2026, 3, 31)
    assert rows[-1].eps_ttm is None
    signals = build_normalized_profit_signals(rows, [], end, close=100.0)
    assert signals.normalized_per_3fy is None
    assert signals.normalized_per_5fy is None
