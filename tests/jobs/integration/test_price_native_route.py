"""Ordinary market acquisition never also acquires legacy daily prices after cutover."""

import json
from datetime import UTC, date, datetime

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.native_index import index_market_originals
from baibai_jobs.acquisition.jquants_market_plan import FixedMarketIndexes
from baibai_jobs.acquisition.jquants_market_receive import receive_market_handoff
from baibai_jobs.price_cutover import price_native_enabled

DAY = date(2026, 11, 2)
AT = datetime(2026, 11, 3, tzinfo=UTC)


def test_native_market_skips_only_legacy_prices_and_retains_other_source_collection(
    tmp_path, monkeypatch
):
    from tests.jobs.acquisition.test_jquants_market_receive import Client

    from baibai_jobs.acquisition import jquants_market_plan as planning

    monkeypatch.setattr(planning, "CALENDAR_LOOKBACK_DAYS", 1)
    monkeypatch.setattr(planning, "CALENDAR_FORWARD_DAYS", 1)
    monkeypatch.setattr(planning, "WEEKLY_BALANCE_LOOKBACK_DAYS", 1)
    monkeypatch.setattr(planning, "MARKET_REVISION_OVERLAP_DAYS", 0)
    monkeypatch.setattr(planning, "ALL_ISSUES_DAILY_FIRST_BALANCE_DATE", DAY)
    indexes = {
        name: index_market_originals([], source=name)
        for name in (
            "master",
            "market_calendar",
            "margin_interest",
            "margin_alerts",
            "short_sale_reports",
        )
    }
    fixed = FixedMarketIndexes("release", "a" * 64, "partial", AT, indexes, (), ())
    # This fixture's master row date is 11/4, independent of native prices.
    from tests.jobs.acquisition.test_jquants_market_receive import ASOF
    from tests.jobs.acquisition.test_jquants_market_receive import AT as RECEIVED_AT

    monkeypatch.setattr(planning, "ALL_ISSUES_DAILY_FIRST_BALANCE_DATE", ASOF)
    assert receive_market_handoff(
        tmp_path / "stage",
        fixed,
        asof=ASOF,
        client=Client(),
        clock=lambda: RECEIVED_AT,
        include_daily_prices=False,
    )
    import sqlite3

    with sqlite3.connect(tmp_path / "stage" / "receipts.sqlite") as connection:
        sources = {
            row[0] for row in connection.execute("SELECT DISTINCT source FROM jquants_source_pages")
        }
    assert "daily_bars" not in sources
    assert {"master", "market_calendar", "margin_alerts", "short_sale_reports"} <= sources


def test_price_rollout_rejects_invalid_values_and_fixed_plan_mode_mismatch(tmp_path, monkeypatch):
    from baibai_jobs.integration.sources import apply

    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "TRUE")
    with pytest.raises(ValueError, match="must be true or false"):
        price_native_enabled()
    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "true")
    (tmp_path / "plan.json").write_text(json.dumps({"price_native": False, "handoffs": []}))
    with pytest.raises(ValueError, match="fixed integration plan"):
        apply(
            MemoryR2Store(),
            tmp_path,
            market=tmp_path / "never-open",
            economic=tmp_path / "never-open-economic",
            asof=DAY,
        )


def test_halted_shared_quota_never_starts_another_market_get(tmp_path):
    class NoMoreRequests:
        def pages(self, *args):
            pytest.fail("the same J-Quants quota group continued after final 429")

    sources = (
        "master",
        "market_calendar",
        "margin_interest",
        "margin_alerts",
        "short_sale_reports",
    )
    fixed = FixedMarketIndexes(
        "release",
        "a" * 64,
        "partial",
        AT,
        {source: index_market_originals([], source=source) for source in sources},
        (),
        (),
    )
    assert not receive_market_handoff(
        tmp_path / "stage",
        fixed,
        asof=DAY,
        client=NoMoreRequests(),
        include_daily_prices=False,
        provider_halted=True,
        clock=lambda: AT,
    )
    report = json.loads((tmp_path / "stage" / "result.json").read_bytes())
    assert "provider_halted" in report["acquisition"]["failed_sources"]
    assert report["request_ids"] == []
