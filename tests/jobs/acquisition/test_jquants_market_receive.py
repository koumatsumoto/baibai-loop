from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.native_index import index_market_originals
from baibai_jobs.acquisition.jquants_market_plan import FixedMarketIndexes
from baibai_jobs.acquisition.jquants_market_receive import receive_market_handoff
from baibai_jobs.integration.source_handoff import _replay

ASOF = date(2026, 11, 4)
AT = datetime(2026, 11, 4, 3, tzinfo=UTC)


class Client:
    def pages(self, source, params):
        if source == "master":
            rows = [
                {
                    "Date": ASOF.isoformat(),
                    "Code": f"{number}0",
                    "CoName": "A",
                    "MktNm": "Prime",
                    "S33Nm": "Other",
                    "ProdCat": "011",
                }
                for number in range(1000, 3500)
            ]
        elif source == "market_calendar":
            first, last = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
            rows = [
                {"Date": (first + timedelta(days=offset)).isoformat(), "HolDiv": "1"}
                for offset in range((last - first).days + 1)
            ]
        else:
            rows = []
        yield JQuantsPage(source, params, AT, json.dumps({"data": rows}).encode())


def test_market_handoff_uses_only_originals_and_replays(tmp_path, monkeypatch):
    from baibai_jobs.acquisition import jquants_market_plan as planning

    monkeypatch.setattr(planning, "FINANCIAL_HISTORY_DAYS", 1)
    monkeypatch.setattr(planning, "CALENDAR_LOOKBACK_DAYS", 1)
    monkeypatch.setattr(planning, "CALENDAR_FORWARD_DAYS", 1)
    monkeypatch.setattr(planning, "WEEKLY_BALANCE_LOOKBACK_DAYS", 1)
    monkeypatch.setattr(planning, "MARKET_REVISION_OVERLAP_DAYS", 0)
    monkeypatch.setattr(planning, "ALL_ISSUES_DAILY_FIRST_BALANCE_DATE", ASOF)
    sources = (
        "master",
        "daily_bars",
        "market_calendar",
        "margin_interest",
        "margin_alerts",
        "short_sale_reports",
    )
    indexes = {source: index_market_originals([], source=source) for source in sources}
    fixed = FixedMarketIndexes("release", "0" * 64, "partial", AT, indexes, (), ())
    stage = tmp_path / "stage"
    assert receive_market_handoff(stage, fixed, asof=ASOF, client=Client(), clock=lambda: AT)
    with sqlite3.connect(stage / "receipts.sqlite") as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("jquants_source_pages",)
        ]
        sources_seen = {
            row[0] for row in conn.execute("SELECT DISTINCT source FROM jquants_source_pages")
        }
    assert sources_seen == {
        "master",
        "daily_bars",
        "market_calendar",
        "margin_alerts",
        "short_sale_reports",
    }
    assert len(list(stage.glob("dependency-*.json"))) == 2
    canonical = tmp_path / "canonical.sqlite"
    assert _replay("jquants-market", canonical, stage).complete


def test_market_source_failure_keeps_partial_and_continues_other_sources(tmp_path, monkeypatch):
    from baibai_engine.data_collection.providers.jquants.contracts import JQuantsAcquisitionError
    from baibai_jobs.acquisition import jquants_market_plan as planning

    monkeypatch.setattr(planning, "FINANCIAL_HISTORY_DAYS", 0)
    monkeypatch.setattr(planning, "CALENDAR_LOOKBACK_DAYS", 0)
    monkeypatch.setattr(planning, "CALENDAR_FORWARD_DAYS", 0)
    monkeypatch.setattr(planning, "WEEKLY_BALANCE_LOOKBACK_DAYS", 0)
    monkeypatch.setattr(planning, "MARKET_REVISION_OVERLAP_DAYS", 0)
    monkeypatch.setattr(planning, "ALL_ISSUES_DAILY_FIRST_BALANCE_DATE", ASOF)
    sources = (
        "master",
        "daily_bars",
        "market_calendar",
        "margin_interest",
        "margin_alerts",
        "short_sale_reports",
    )
    indexes = {source: index_market_originals([], source=source) for source in sources}
    fixed = FixedMarketIndexes("release", "0" * 64, "partial", AT, indexes, (), ())

    class PartialClient(Client):
        def pages(self, source, params):
            if source == "master":
                yield JQuantsPage(source, params, AT, b'{"data":[],"pagination_key":"next"}')
                raise JQuantsAcquisitionError("stopped")
            yield from super().pages(source, params)

    stage = tmp_path / "stage"
    assert not receive_market_handoff(
        stage, fixed, asof=ASOF, client=PartialClient(), clock=lambda: AT
    )
    report = json.loads((stage / "result.json").read_text())
    assert "master" in report["acquisition"]["failed_sources"]
    with sqlite3.connect(stage / "receipts.sqlite") as conn:
        sources_seen = {
            row[0] for row in conn.execute("SELECT DISTINCT source FROM jquants_source_pages")
        }
    assert {"master", "daily_bars", "market_calendar", "short_sale_reports"} <= sources_seen


def test_scheduled_closed_market_does_not_request_sources(tmp_path):
    from baibai_engine.data_collection.providers.jquants.contracts import (
        RetainedRequest,
        complete_request,
    )
    from baibai_engine.data_collection.providers.jquants.dependencies import dependency_from_request

    page = JQuantsPage(
        "market_calendar",
        {"from": "20261104", "to": "20261104"},
        AT,
        b'{"data":[{"Date":"2026-11-04","HolDiv":"0"}]}',
    )
    request = complete_request([page])
    sources = (
        "master",
        "daily_bars",
        "market_calendar",
        "margin_interest",
        "margin_alerts",
        "short_sale_reports",
    )
    indexes = {source: index_market_originals([], source=source) for source in sources}
    indexes["market_calendar"] = index_market_originals(
        [RetainedRequest("cal", request, None)], source="market_calendar"
    )
    fixed = FixedMarketIndexes(
        "release", "0" * 64, "partial", AT, indexes, (), (dependency_from_request("cal", request),)
    )

    class NoRequests:
        def pages(self, source, params):
            raise AssertionError("closed scheduled market must not request data")

    stage = tmp_path / "closed"
    assert receive_market_handoff(
        stage, fixed, asof=ASOF, client=NoRequests(), scheduled=True, clock=lambda: AT
    )
    assert (
        json.loads((stage / "result.json").read_text())["acquisition"]["status"]
        == "skipped_non_business_day"
    )
    assert len(list(stage.glob("dependency-*.json"))) == 1


def test_rate_limit_stops_remaining_requests_and_keeps_received_page(tmp_path, monkeypatch):
    from baibai_engine.data_collection.providers.jquants.contracts import JQuantsRateLimitError
    from baibai_jobs.acquisition import jquants_market_plan as planning

    monkeypatch.setattr(planning, "FINANCIAL_HISTORY_DAYS", 0)
    monkeypatch.setattr(planning, "CALENDAR_LOOKBACK_DAYS", 0)
    monkeypatch.setattr(planning, "CALENDAR_FORWARD_DAYS", 0)
    monkeypatch.setattr(planning, "WEEKLY_BALANCE_LOOKBACK_DAYS", 0)
    monkeypatch.setattr(planning, "MARKET_REVISION_OVERLAP_DAYS", 0)
    monkeypatch.setattr(planning, "ALL_ISSUES_DAILY_FIRST_BALANCE_DATE", ASOF)
    sources = (
        "master",
        "daily_bars",
        "market_calendar",
        "margin_interest",
        "margin_alerts",
        "short_sale_reports",
    )
    indexes = {source: index_market_originals([], source=source) for source in sources}
    fixed = FixedMarketIndexes("release", "0" * 64, "partial", AT, indexes, (), ())

    class RateLimited:
        def __init__(self):
            self.called = []

        def pages(self, source, params):
            self.called.append(source)
            yield JQuantsPage(source, params, AT, b'{"data":[],"pagination_key":"next"}')
            raise JQuantsRateLimitError("rate limit")

    client = RateLimited()
    stage = tmp_path / "rate-limited"
    assert not receive_market_handoff(stage, fixed, asof=ASOF, client=client, clock=lambda: AT)
    assert client.called == ["master"]
    with sqlite3.connect(stage / "receipts.sqlite") as conn:
        assert conn.execute("SELECT count(*) FROM jquants_source_pages").fetchone()[0] == 1
