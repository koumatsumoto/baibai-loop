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
from baibai_jobs.margin_interest_cutover import margin_interest_native_enabled


def test_mode_defaults_off_and_fixed_plan_cannot_change(tmp_path, monkeypatch):
    from baibai_jobs.integration.sources import apply

    monkeypatch.delenv("BAIBAI_MARGIN_INTEREST_NATIVE", raising=False)
    assert not margin_interest_native_enabled()
    monkeypatch.setenv("BAIBAI_MARGIN_INTEREST_NATIVE", "yes")
    with pytest.raises(ValueError, match="true or false"):
        margin_interest_native_enabled()
    monkeypatch.setenv("BAIBAI_MARGIN_INTEREST_NATIVE", "true")
    for name in (
        "FINANCIAL",
        "PRICE",
        "MASTER",
        "CALENDAR",
        "EDINET_LIST",
        "MARGIN_ALERTS",
        "SHORT_SALE_REPORTS",
    ):
        monkeypatch.setenv(f"BAIBAI_{name}_NATIVE", "false")
    (tmp_path / "plan.json").write_text(json.dumps({"handoffs": []}))
    with pytest.raises(
        ValueError, match="margin interest mode differs from the fixed integration plan"
    ):
        apply(MemoryR2Store(), tmp_path, market=tmp_path / "not-opened", asof=DAY)
    assert not (tmp_path / "not-opened").exists()


def test_all_native_market_commits_margin_without_legacy_lake_or_stage(tmp_path, monkeypatch):
    from tests.jobs.acquisition.test_jquants_margin_interest_native import Client

    from baibai_engine.data_collection.providers.jquants import http
    from baibai_engine.storage.snapshots import objects
    from baibai_jobs.acquisition import (
        jquants_calendar_native,
        jquants_margin_alerts_native,
        jquants_master_native,
        jquants_price_native,
        jquants_short_sale_reports_native,
    )
    from baibai_jobs.integration import source_handoff as route

    for name in (
        "PRICE",
        "MASTER",
        "CALENDAR",
        "MARGIN_ALERTS",
        "SHORT_SALE_REPORTS",
        "MARGIN_INTEREST",
    ):
        monkeypatch.setenv(f"BAIBAI_{name}_NATIVE", "true")
    store = MemoryR2Store()
    store.close = lambda: None
    asof = date(2026, 9, 28)
    first = asof - timedelta(days=230)
    fixed_calendar = calendar(store, tmp_path / "calendar", first, asof)
    MarginInterestL1(store, tmp_path / "init").initialize()
    client = Client()
    monkeypatch.setattr(http, "JQuantsHTTP", lambda *_: client)
    monkeypatch.setattr(route, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(jquants_calendar_native, "calendar_window", lambda *a: (first, asof))
    for module, name in (
        (jquants_calendar_native, "acquire_calendar_native"),
        (jquants_master_native, "acquire_master_native"),
        (jquants_price_native, "acquire_price_native"),
        (jquants_margin_alerts_native, "acquire_margin_alerts_native"),
        (jquants_short_sale_reports_native, "acquire_short_sale_reports_native"),
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
    assert all(
        source == "margin_interest" and len(query["date"]) == 8 for source, query in client.calls
    )
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


def test_legacy_market_excludes_only_native_margin(tmp_path, monkeypatch):
    from tests.jobs.acquisition.test_jquants_market_receive import ASOF, Client
    from tests.jobs.acquisition.test_jquants_market_receive import AT as RECEIVED

    from baibai_engine.data_collection.providers.jquants.native_index import index_market_originals
    from baibai_jobs.acquisition import jquants_market_plan as planning
    from baibai_jobs.acquisition.jquants_market_receive import receive_market_handoff

    for name in (
        "FINANCIAL_HISTORY_DAYS",
        "CALENDAR_LOOKBACK_DAYS",
        "CALENDAR_FORWARD_DAYS",
        "MARKET_REVISION_OVERLAP_DAYS",
    ):
        monkeypatch.setattr(planning, name, 0)
    fixed = planning.FixedMarketIndexes(
        "release",
        "a" * 64,
        "partial",
        RECEIVED,
        {
            name: index_market_originals([], source=name)
            for name in MARKET_SOURCES
            if name != "margin_interest"
        },
        (),
        (),
    )
    calls = []

    class RecordingClient(Client):
        def pages(self, source, params):
            calls.append(source)
            yield from super().pages(source, params)

    assert receive_market_handoff(
        tmp_path / "legacy",
        fixed,
        asof=ASOF,
        client=RecordingClient(),
        clock=lambda: RECEIVED,
        include_margin_interest=False,
    )
    assert "margin_interest" not in calls
    assert {
        "master",
        "daily_bars",
        "market_calendar",
        "margin_alerts",
        "short_sale_reports",
    } <= set(calls)


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


def test_native_margin_requires_calendar_and_blocks_old_writer_before_http(tmp_path, monkeypatch):
    from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
    from baibai_jobs.integration.source_handoff import _acquire_market_l1
    from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh

    monkeypatch.setenv("BAIBAI_MARGIN_INTEREST_NATIVE", "true")
    monkeypatch.setenv("BAIBAI_CALENDAR_NATIVE", "false")
    with pytest.raises(ValueError, match="requires native calendar"):
        _acquire_market_l1(DAY, tmp_path)
    refresh = object.__new__(JQuantsRefresh)
    for method in (
        refresh.ensure_weekly_margin,
        refresh.refresh_weekly_margin,
        refresh.ensure_all_issues_daily_margin,
    ):
        with pytest.raises(JQuantsProviderError, match="legacy margin interest writer is disabled"):
            method(DAY)
