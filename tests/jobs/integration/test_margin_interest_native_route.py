"""Ordinary margin acquisition and old-stage replay keep L1 independent of L2."""

import json
from datetime import date, timedelta

import pytest
from tests.helpers.margin_interest_native import AT, DAY, calendar
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.margin_interest_native import MarginInterestL1
from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    ShortSaleReportsL1,
)


def test_native_market_commits_all_sources_without_legacy_lake(tmp_path, monkeypatch):
    from tests.jobs.acquisition.test_jquants_margin_interest_native import Client

    from baibai_engine.data_collection.providers.jquants import http
    from baibai_engine.storage.snapshots import objects
    from baibai_jobs.acquisition import (
        jquants_calendar_native,
        jquants_master_native,
        jquants_price_native,
    )
    from baibai_jobs.integration import source_handoff as route

    store = MemoryR2Store()
    store.close = lambda: None
    asof = date(2026, 9, 28)
    first = asof - timedelta(days=230)
    fixed_calendar = calendar(store, tmp_path / "calendar", first, asof)
    MarginInterestL1(store, tmp_path / "init").initialize()
    from baibai_engine.data_collection.providers.jquants.margin_alerts_native import MarginAlertsL1

    MarginAlertsL1(store, tmp_path / "alerts").initialize()
    ShortSaleReportsL1(store, tmp_path / "shorts").initialize()

    class MarketClient(Client):
        def pages(self, source, query):
            if source == "margin_interest":
                yield from super().pages(source, query)
            else:
                self.calls.append((source, query))
                yield JQuantsPage(source, query, AT, b'{"data":[]}')

    client = MarketClient()
    monkeypatch.setattr(http, "JQuantsHTTP", lambda *_: client)
    monkeypatch.setattr(route, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(jquants_calendar_native, "calendar_window", lambda *a: (first, asof))
    for module, name in (
        (jquants_calendar_native, "acquire_calendar_native"),
        (jquants_master_native, "acquire_master_native"),
        (jquants_price_native, "acquire_price_native"),
    ):
        monkeypatch.setattr(
            module, name, lambda *a, **kw: {"failed": 0, "held": 0, "incomplete": 0}
        )
    monkeypatch.setattr(
        objects, "open_lake", lambda **_: pytest.fail("legacy lake is not an input")
    )
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    directory = tmp_path / "cycle"
    assert (
        route.main(
            [
                "acquire",
                "--source",
                "jquants-market",
                "--asof",
                asof.isoformat(),
                "--directory",
                str(directory),
            ]
        )
        == 0
    )
    assert output.read_text() == "stage_ready=false\nnative_l1=true\n"
    assert not (directory / "receipts.sqlite").exists()
    assert client.calls
    assert {source for source, _ in client.calls} == {
        "margin_interest",
        "margin_alerts",
        "short_sale_reports",
    }
    for source, query in client.calls:
        if source == "margin_interest":
            assert len(query["date"]) == 8
        else:
            assert len(query["disc_date" if source == "short_sale_reports" else "date"]) == 10
    assert len(MarginAlertsL1(store, tmp_path / "cold-alerts").fixed().value()["scopes"]) == 8
    assert len(ShortSaleReportsL1(store, tmp_path / "cold-shorts").fixed().value()["scopes"]) == 8
    report = json.loads((directory / "result.json").read_bytes())
    interest = report["independent_l1"]["margin_interest"]
    assert interest["calendar_sha256"] == fixed_calendar.content_id
    assert interest["remaining"] == {
        "days": [],
        "unknown_calendar_days": [],
        "unknown_incomplete_requests": [],
    }
    assert (
        MarginInterestL1(store, tmp_path / "read")
        .fixed()
        .value()["scopes"][DAY.isoformat()]["adopted"]
    )
