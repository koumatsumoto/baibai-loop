"""Ordinary short-report routing stays independent of L2 and exact on old stages."""

import gzip
import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import replace

import pytest
from tests.helpers.jquants_receipts import body
from tests.helpers.margin_alerts_native import attempt as alert_attempt
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.short_sale_reports_native import AT, DAY, attempt, records

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsPage
from baibai_engine.data_collection.providers.jquants.margin_alerts_native import MarginAlertsL1
from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    ShortSaleReportsL1,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.jquants_handoff import MARKET_SOURCES, import_handoff
from baibai_jobs.short_sale_reports_cutover import short_sale_reports_native_enabled


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


def test_mode_defaults_off_and_fixed_plan_cannot_change(tmp_path, monkeypatch):
    from baibai_jobs.integration.sources import apply

    monkeypatch.delenv("BAIBAI_SHORT_SALE_REPORTS_NATIVE", raising=False)
    assert not short_sale_reports_native_enabled()
    monkeypatch.setenv("BAIBAI_SHORT_SALE_REPORTS_NATIVE", "yes")
    with pytest.raises(ValueError, match="true or false"):
        short_sale_reports_native_enabled()
    monkeypatch.setenv("BAIBAI_SHORT_SALE_REPORTS_NATIVE", "true")
    (tmp_path / "plan.json").write_text(json.dumps({"handoffs": []}))
    with pytest.raises(ValueError, match="fixed integration plan"):
        apply(MemoryR2Store(), tmp_path, market=tmp_path / "not-opened", asof=DAY)
    assert not (tmp_path / "not-opened").exists()


def test_ordinary_short_acquisition_survives_missing_l2_and_keeps_iso_disc_date(
    tmp_path, monkeypatch
):
    from datetime import timedelta

    from baibai_engine.data_collection.providers.jquants import http
    from baibai_engine.storage.snapshots import objects
    from baibai_jobs.integration import source_handoff as route

    monkeypatch.setenv("BAIBAI_SHORT_SALE_REPORTS_NATIVE", "true")
    store = MemoryR2Store()
    store.close = lambda: None
    ShortSaleReportsL1(store, tmp_path / "init").initialize()
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
        ("short_sale_reports", {"disc_date": (DAY - timedelta(days=n)).isoformat()})
        for n in range(7, -1, -1)
    ]
    fixed = ShortSaleReportsL1(store, tmp_path / "cold").fixed()
    assert len(fixed.value()["scopes"]) == 8
    assert all(scope.get("adopted") for scope in fixed.value()["scopes"].values())
    assert (tmp_path / "cycle/short-sale-reports-native-result.json").is_file()


@pytest.mark.parametrize("migrated_alerts", [False, True])
@pytest.mark.parametrize("partial", [False, True])
def test_mixed_stage_only_skips_verified_migrated_sources(tmp_path, migrated_alerts, partial):
    store = MemoryR2Store()
    owner = ShortSaleReportsL1(store, tmp_path / "short-native")
    owner.initialize()
    short = replace(attempt("short", partial=partial), attempted_at=None, failure=None)
    owner.commit([short])
    alert_owner = MarginAlertsL1(store, tmp_path / "alert-native")
    alert_owner.initialize()
    alert = replace(alert_attempt("alert"), attempted_at=None)
    alert_owner.commit([alert])
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    with closing(open_connection(source)) as connection, connection:
        receive_page(connection, short.pages[0], request_id="short", page_index=0)
        receive_page(connection, alert.pages[0], request_id="alert", page_index=0)
    fingerprints = {
        key: receipt["fingerprint"] for key, receipt in owner.fixed().value()["receipts"].items()
    }
    alert_fingerprints = {
        key: receipt["fingerprint"]
        for key, receipt in alert_owner.fixed().value()["receipts"].items()
    }
    result = import_handoff(
        source,
        target,
        ("short", "alert"),
        sources=MARKET_SOURCES,
        short_sale_reports_fingerprints=fingerprints,
        margin_alerts_fingerprints=alert_fingerprints if migrated_alerts else None,
    )
    assert result.complete
    assert result.report["migrated_short_sale_reports_requests"] == ["short"]
    assert result.report["replayed_requests"] == ([] if migrated_alerts else ["alert"])
    with sqlite3.connect(target) as connection:
        assert connection.execute(
            "SELECT DISTINCT source FROM jquants_source_pages"
        ).fetchall() == ([] if migrated_alerts else [("margin_alerts",)])


@pytest.mark.parametrize("damage", ["absent", "body", "clock", "page_gap", "filtered"])
def test_short_stage_fingerprint_mismatch_is_not_dropped(tmp_path, damage):
    from datetime import timedelta

    store = MemoryR2Store()
    owner = ShortSaleReportsL1(store, tmp_path / "native")
    owner.initialize()
    original = replace(attempt("short"), attempted_at=None)
    owner.commit([original])
    fingerprints = {
        key: receipt["fingerprint"] for key, receipt in owner.fixed().value()["receipts"].items()
    }
    page, index = original.pages[0], 0
    if damage == "absent":
        fingerprints = {}
    elif damage == "body":
        page = JQuantsPage(page.source, page.query, page.received_at, body(records(name="changed")))
    elif damage == "clock":
        page = JQuantsPage(page.source, page.query, AT + timedelta(seconds=1), page.body)
    elif damage == "page_gap":
        index = 1
    else:
        page = JQuantsPage(page.source, {"date": DAY.isoformat()}, page.received_at, page.body)
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    with closing(open_connection(source)) as connection, connection:
        receive_page(connection, page, request_id="short", page_index=index)
    with pytest.raises(ValueError, match="legacy short_sale_reports"):
        import_handoff(
            source,
            target,
            ("short",),
            sources=MARKET_SOURCES,
            short_sale_reports_fingerprints=fingerprints,
        )
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM jquants_source_pages").fetchone()[0] == 0


def test_legacy_short_writer_refuses_before_fetch_when_native_enabled(tmp_path, monkeypatch):
    from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
    from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh

    monkeypatch.setenv("BAIBAI_SHORT_SALE_REPORTS_NATIVE", "true")
    refresh = object.__new__(JQuantsRefresh)
    for method in (refresh.ensure_short_sale_reports, refresh.refresh_short_sale_reports):
        with pytest.raises(
            JQuantsProviderError, match="legacy short-sale reports writer is disabled"
        ):
            method(DAY, DAY)


def test_native_quota_halt_stays_durable_and_stops_remaining_legacy_acquisition(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from baibai_engine.data_collection.providers.jquants import http
    from baibai_engine.data_collection.providers.jquants.contracts import JQuantsRateLimitError
    from baibai_engine.storage.snapshots import objects, reader
    from baibai_jobs.acquisition import jquants_market_plan, jquants_market_receive
    from baibai_jobs.integration import source_handoff as route

    monkeypatch.setenv("BAIBAI_SHORT_SALE_REPORTS_NATIVE", "true")
    store = MemoryR2Store()
    store.close = lambda: None
    ShortSaleReportsL1(store, tmp_path / "init").initialize()
    calls = []

    class Client:
        def __init__(self, *_):
            pass

        def close(self):
            pass

        def pages(self, source, params):
            calls.append((source, params))
            raise JQuantsRateLimitError("synthetic provider quota")
            yield

    @contextmanager
    def local_lake(**_):
        yield None, SimpleNamespace(source=object())

    def remaining(directory, fixed, **kwargs):
        assert kwargs["provider_halted"]
        assert not kwargs["include_short_sales"]
        (directory / "result.json").write_text(json.dumps({"acquisition": {"status": "failed"}}))
        return False

    monkeypatch.setattr(http, "JQuantsHTTP", Client)
    monkeypatch.setattr(route, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(objects, "open_lake", local_lake)
    monkeypatch.setattr(reader, "resolve_current_release", lambda *a, **kw: object())
    monkeypatch.setattr(jquants_market_plan, "freeze_market_originals", lambda *a, **kw: object())
    monkeypatch.setattr(jquants_market_receive, "receive_market_handoff", remaining)
    assert not route._acquire_market_l1(DAY, tmp_path / "cycle")
    assert len(calls) == 1
    report = json.loads((tmp_path / "cycle/result.json").read_bytes())
    assert report["independent_l1"]["short_sale_reports"]["halted"] == 1
    fixed = ShortSaleReportsL1(store, tmp_path / "cold").fixed()
    assert len(fixed.value()["scopes"]) == 1
    assert next(iter(fixed.value()["scopes"].values()))["failure"]
