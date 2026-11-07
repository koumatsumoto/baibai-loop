"""Ordinary margin acquisition and old-stage replay keep L1 independent of L2."""

import json
from contextlib import closing
from dataclasses import replace
from datetime import date, timedelta

import pytest
from tests.helpers.margin_interest_native import AT, DAY, attempt, calendar
from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.integration.test_margin_alerts_native_route import receive_page

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.margin_interest_native import MarginInterestL1
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.jquants_handoff import MARKET_SOURCES, import_handoff


@pytest.mark.parametrize("obsolete_value", [None, "false", "yes"])
def test_native_market_ignores_retired_flags_and_never_opens_legacy_lake(
    tmp_path, monkeypatch, obsolete_value
):
    from tests.jobs.acquisition.test_jquants_margin_interest_native import Client

    from baibai_engine.data_collection.providers.jquants import http
    from baibai_engine.storage.snapshots import objects
    from baibai_jobs.acquisition import (
        jquants_calendar_native,
        jquants_master_native,
        jquants_price_native,
    )
    from baibai_jobs.integration import source_handoff as route

    for name in (
        "PRICE",
        "MASTER",
        "CALENDAR",
    ):
        monkeypatch.setenv(f"BAIBAI_{name}_NATIVE", "true")
    for source in ("MARGIN_ALERTS", "SHORT_SALE_REPORTS", "MARGIN_INTEREST"):
        if obsolete_value is None:
            monkeypatch.delenv(f"BAIBAI_{source}_NATIVE", raising=False)
        else:
            monkeypatch.setenv(f"BAIBAI_{source}_NATIVE", obsolete_value)
    store = MemoryR2Store()
    store.close = lambda: None
    asof = date(2026, 9, 28)
    first = asof - timedelta(days=230)
    fixed_calendar = calendar(store, tmp_path / "calendar", first, asof)
    MarginInterestL1(store, tmp_path / "init").initialize()
    from baibai_engine.data_collection.providers.jquants.margin_alerts_native import MarginAlertsL1
    from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
        ShortSaleReportsL1,
    )

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


@pytest.mark.parametrize("damage", [None, "absent", "body", "clock"])
def test_mixed_stage_skips_only_proven_margin_receipt(tmp_path, damage):
    store = MemoryR2Store()
    owner = MarginInterestL1(store, tmp_path / "native")
    owner.initialize()
    original = replace(attempt("margin"), attempted_at=None)
    owner.commit([original])
    fingerprints = {
        key: row["fingerprint"] for key, row in owner.fixed().value()["receipts"].items()
    }
    page = original.pages[0]
    if damage == "absent":
        fingerprints = {}
    elif damage == "body":
        page = replace(page, body=b'{"data":[]}')
    elif damage == "clock":
        page = replace(page, received_at=AT + timedelta(seconds=1))
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    with closing(open_connection(source)) as connection, connection:
        receive_page(connection, page, request_id="margin", page_index=0)
        receive_page(
            connection,
            JQuantsPage("short_sale_reports", {"disc_date": DAY.isoformat()}, AT, b'{"data":[]}'),
            request_id="short",
            page_index=0,
        )
    if damage:
        with pytest.raises(ValueError, match="legacy margin_interest"):
            import_handoff(
                source,
                target,
                ("margin", "short"),
                sources=MARKET_SOURCES,
                margin_interest_fingerprints=fingerprints,
            )
    else:
        result = import_handoff(
            source,
            target,
            ("margin", "short"),
            sources=MARKET_SOURCES,
            margin_interest_fingerprints=fingerprints,
        )
        assert result.complete
        assert result.report["migrated_margin_interest_requests"] == ["margin"]
        assert result.report["replayed_requests"] == ["short"]
        with closing(open_connection(target)) as connection:
            assert connection.execute(
                "SELECT DISTINCT source FROM jquants_source_pages"
            ).fetchall() == [("short_sale_reports",)]


@pytest.mark.parametrize(
    "disabled", [("PRICE",), ("MASTER",), ("CALENDAR",), ("PRICE", "MASTER", "CALENDAR")]
)
def test_market_native_dependencies_never_fall_back_to_legacy(tmp_path, monkeypatch, disabled):
    from baibai_jobs.integration import source_handoff

    for name in ("PRICE", "MASTER", "CALENDAR"):
        monkeypatch.setenv(f"BAIBAI_{name}_NATIVE", "false" if name in disabled else "true")
    monkeypatch.setattr(
        source_handoff,
        "Boto3R2Store",
        lambda **kw: pytest.fail("dependency failure must precede store access"),
    )
    with pytest.raises(ValueError, match="requires native calendar, master and prices"):
        source_handoff._acquire_market_l1(DAY, tmp_path)
