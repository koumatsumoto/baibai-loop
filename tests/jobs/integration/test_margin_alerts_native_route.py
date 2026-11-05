"""Margin-alert native routing preserves independent acquisition and mixed stages."""

import gzip
import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from tests.helpers.margin_alerts_native import AT, DAY, attempt, records
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.margin_alerts_native import MarginAlertsL1
from baibai_engine.data_collection.providers.jquants.native_index import index_market_originals
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.jquants_market_plan import FixedMarketIndexes
from baibai_jobs.acquisition.jquants_market_receive import receive_market_handoff
from baibai_jobs.integration.jquants_handoff import MARKET_SOURCES, import_handoff
from baibai_jobs.margin_alerts_cutover import margin_alerts_native_enabled


def receive_page(connection, page, *, request_id, page_index):
    connection.execute(
        "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
        (
            request_id,
            page_index,
            page.source,
            json.dumps(dict(page.query)),
            page.received_at.isoformat(),
            page.received_at.date().isoformat(),
            gzip.compress(page.body, mtime=0),
        ),
    )


def test_flag_requires_explicit_rollout_and_fixed_plan_match(tmp_path, monkeypatch):
    from baibai_jobs.integration.sources import apply

    monkeypatch.delenv("BAIBAI_MARGIN_ALERTS_NATIVE", raising=False)
    assert not margin_alerts_native_enabled()
    monkeypatch.setenv("BAIBAI_MARGIN_ALERTS_NATIVE", "yes")
    with pytest.raises(ValueError, match="true or false"):
        margin_alerts_native_enabled()
    monkeypatch.setenv("BAIBAI_MARGIN_ALERTS_NATIVE", "true")
    (tmp_path / "plan.json").write_text(json.dumps({"handoffs": []}))
    with pytest.raises(ValueError, match="fixed integration plan"):
        apply(MemoryR2Store(), tmp_path, market=tmp_path / "never-open", asof=DAY)


def test_native_market_excludes_alerts_only_and_keeps_short_sale_and_margin(tmp_path, monkeypatch):
    from tests.jobs.acquisition.test_jquants_market_receive import ASOF, Client
    from tests.jobs.acquisition.test_jquants_market_receive import AT as RECEIVED

    from baibai_jobs.acquisition import jquants_market_plan as planning

    for name in (
        "FINANCIAL_HISTORY_DAYS",
        "CALENDAR_LOOKBACK_DAYS",
        "CALENDAR_FORWARD_DAYS",
        "WEEKLY_BALANCE_LOOKBACK_DAYS",
        "MARKET_REVISION_OVERLAP_DAYS",
    ):
        monkeypatch.setattr(planning, name, 0)
    monkeypatch.setattr(planning, "ALL_ISSUES_DAILY_FIRST_BALANCE_DATE", ASOF)
    names = tuple(name for name in MARKET_SOURCES if name != "margin_alerts")
    fixed = FixedMarketIndexes(
        "release",
        "a" * 64,
        "partial",
        RECEIVED,
        {name: index_market_originals([], source=name) for name in names},
        (),
        (),
    )
    calls = []

    class RecordingClient(Client):
        def pages(self, source, params):
            calls.append(source)
            yield from super().pages(source, params)

    assert receive_market_handoff(
        tmp_path / "stage",
        fixed,
        asof=ASOF,
        client=RecordingClient(),
        clock=lambda: RECEIVED,
        include_margin_alerts=False,
    )
    assert "margin_alerts" not in calls
    assert {"master", "daily_bars", "market_calendar", "short_sale_reports"} <= set(calls)


def test_alerts_commit_before_missing_legacy_lake_and_do_not_require_l2(tmp_path, monkeypatch):
    from baibai_engine.data_collection.providers.jquants import http
    from baibai_engine.storage.snapshots import objects
    from baibai_jobs.integration import source_handoff as route

    monkeypatch.setenv("BAIBAI_MARGIN_ALERTS_NATIVE", "true")
    store = MemoryR2Store()
    store.close = lambda: None
    MarginAlertsL1(store, tmp_path / "init").initialize()
    calls = []

    class Client:
        def __init__(self, *_):
            pass

        def close(self):
            pass

        def pages(self, source, params):
            calls.append((source, params))
            yield JQuantsPage(source, params, AT, b'{"data":[]}')

    @contextmanager
    def unavailable(**_):
        raise ValueError("legacy lake unavailable")
        yield

    monkeypatch.setattr(http, "JQuantsHTTP", Client)
    monkeypatch.setattr(route, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(objects, "open_lake", unavailable)
    with pytest.raises(ValueError, match="legacy lake unavailable"):
        route._acquire_market_l1(DAY, tmp_path / "cycle")
    assert calls == [
        ("margin_alerts", {"date": (DAY - timedelta(days=n)).isoformat()}) for n in range(7, -1, -1)
    ]
    fixed = MarginAlertsL1(store, tmp_path / "cold").fixed()
    assert len(fixed.value()["scopes"]) == 8
    assert all(scope.get("adopted") for scope in fixed.value()["scopes"].values())
    assert (tmp_path / "cycle" / "margin-alerts-native-result.json").is_file()


@pytest.mark.parametrize("tamper", [False, True])
def test_mixed_stage_skips_only_exact_migrated_alerts(tmp_path, tamper):
    store = MemoryR2Store()
    owner = MarginAlertsL1(store, tmp_path / "native")
    owner.initialize()
    received = replace(attempt("alert"), attempted_at=None)
    owner.commit([received])
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    with closing(open_connection(source)) as connection, connection:
        receive_page(connection, received.pages[0], request_id="alert", page_index=0)
        short = JQuantsPage(
            "short_sale_reports", {"disc_date": DAY.isoformat()}, AT, b'{"data":[]}'
        )
        receive_page(connection, short, request_id="short", page_index=0)
        if tamper:
            changed = attempt("other", rows=records(long=123))
            connection.execute("DELETE FROM jquants_source_pages WHERE request_id='alert'")
            receive_page(connection, changed.pages[0], request_id="alert", page_index=0)
    fingerprints = {
        key: value["fingerprint"] for key, value in owner.fixed().value()["receipts"].items()
    }
    if tamper:
        with pytest.raises(ValueError, match="differs from the native cutover"):
            import_handoff(
                source,
                target,
                ("alert", "short"),
                sources=MARKET_SOURCES,
                margin_alerts_fingerprints=fingerprints,
            )
    else:
        result = import_handoff(
            source,
            target,
            ("alert", "short"),
            sources=MARKET_SOURCES,
            margin_alerts_fingerprints=fingerprints,
        )
        assert result.complete
        assert result.report["migrated_margin_alerts_requests"] == ["alert"]
        assert result.report["replayed_requests"] == ["short"]
        with sqlite3.connect(target) as connection:
            assert connection.execute(
                "SELECT DISTINCT source FROM jquants_source_pages"
            ).fetchall() == [("short_sale_reports",)]


def test_legacy_maintenance_never_fetches_alerts_in_native_mode(tmp_path, monkeypatch):
    from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
    from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh

    monkeypatch.setenv("BAIBAI_MARGIN_ALERTS_NATIVE", "true")
    refresh = object.__new__(JQuantsRefresh)
    for method in (refresh.ensure_margin_alerts, refresh.refresh_margin_alerts):
        with pytest.raises(JQuantsProviderError, match="legacy margin alerts writer is disabled"):
            method(DAY, DAY)
