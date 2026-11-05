"""Master receipts commit before any legacy release can block their progress."""

import json
from contextlib import contextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.integration.test_master_native_models import records

import baibai_engine.data_collection.providers.jquants.http as http
import baibai_engine.storage.snapshots.objects as objects
import baibai_engine.storage.snapshots.reader as readers
import baibai_jobs.acquisition.jquants_market_plan as planning
import baibai_jobs.integration.source_handoff as handoff
from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    JQuantsRateLimitError,
)
from baibai_engine.data_collection.providers.jquants.master_native import MasterL1
from baibai_engine.data_collection.providers.jquants.native_index import index_market_originals
from baibai_jobs.master_cutover import master_native_enabled

DAY = date(2026, 11, 4)
AT = datetime(2026, 11, 4, 3, tzinfo=UTC)


def setup(tmp_path, monkeypatch, client):
    store = MemoryR2Store()
    store.close = lambda: None
    MasterL1(store, tmp_path / "initialize").initialize()
    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "false")
    monkeypatch.setattr(handoff, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(http, "JQuantsHTTP", lambda *_: client)
    return store


def test_commits_master_before_unavailable_legacy_market_release(tmp_path, monkeypatch):
    class Client:
        def close(self):
            pass

        def pages(self, source, params):
            assert source == "master"
            yield JQuantsPage(source, params, AT, json.dumps({"data": records(DAY)}).encode())

    store = setup(tmp_path, monkeypatch, Client())

    def absent_release(**_):
        raise ValueError("legacy market release unavailable")

    monkeypatch.setattr(objects, "open_lake", absent_release)
    with pytest.raises(ValueError, match="legacy market release unavailable"):
        handoff._acquire_market_l1(DAY, tmp_path / "acquire")
    fixed = MasterL1(store, tmp_path / "cold").fixed()
    assert len(next(fixed.requests(store, days=[DAY])).request.records()) == 2503
    assert json.loads((tmp_path / "acquire/master-native-result.json").read_bytes())["changed"] == 1


def test_final_rate_limit_preserves_failure_and_stops_remaining_shared_quota(tmp_path, monkeypatch):
    calls = []

    class Limited:
        def close(self):
            pass

        def pages(self, source, params):
            calls.append((source, params))
            raise JQuantsRateLimitError("quota exhausted")
            yield

    store = setup(tmp_path, monkeypatch, Limited())

    @contextmanager
    def lake(**_):
        yield None, SimpleNamespace(source=None)

    def frozen(*_, **kwargs):
        assert kwargs["include_master"] is False
        indexes = {
            source: index_market_originals([], source=source)
            for source in (
                "daily_bars",
                "market_calendar",
                "margin_interest",
                "margin_alerts",
                "short_sale_reports",
            )
        }
        return planning.FixedMarketIndexes("fixed", "a" * 64, "partial", AT, indexes, (), ())

    monkeypatch.setattr(objects, "open_lake", lake)
    monkeypatch.setattr(readers, "resolve_current_release", lambda *_, **__: None)
    monkeypatch.setattr(planning, "freeze_market_originals", frozen)
    assert handoff._acquire_market_l1(DAY, tmp_path / "acquire") is False
    assert len(calls) == 1
    assert calls[0][0] == "master"
    fixed = MasterL1(store, tmp_path / "cold").fixed()
    assert fixed.value()["scopes"][DAY.isoformat()]["failure"] is not None


@pytest.mark.parametrize("value", ["TRUE", "1", "yes", " true", "false "])
def test_master_switch_rejects_noncanonical_values(monkeypatch, value):
    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", value)
    with pytest.raises(ValueError, match="must be true or false"):
        master_native_enabled()


def test_legacy_planner_excludes_master_without_claiming_unknown_cross_namespace_ids(
    tmp_path, monkeypatch
):
    @contextmanager
    def pending(_store, *, verify_l1, **_):
        assert verify_l1(frozenset({"native-master", "legacy-calendar"})) == frozenset(
            {"legacy-calendar"}
        )
        yield AT, (), ()

    monkeypatch.setattr(planning, "open_pending_originals", pending)
    monkeypatch.setattr(
        planning, "fixed_complete_request_ids", lambda *_, **__: frozenset({"legacy-calendar"})
    )
    monkeypatch.setattr(planning, "iter_fixed_retained_requests", lambda *_, **__: iter(()))
    monkeypatch.setattr(
        planning, "accepted_dataset", lambda *_: SimpleNamespace(name="jquants.source_pages")
    )
    fixed = planning.freeze_market_originals(
        None,
        release=SimpleNamespace(
            release_id="fixed",
            manifest_sha256="a" * 64,
            dataset_manifest=lambda *_: SimpleNamespace(coverage_status="partial"),
        ),
        cache=None,
        store=None,
        asof=DAY,
        include_master=False,
        clock=lambda: AT,
    )
    assert "master" not in fixed.indexes


@pytest.mark.parametrize(("division", "expected"), [("0", True), ("3", True), ("unknown", False)])
def test_confirmed_closed_empty_is_retained_without_false_cycle_failure(
    tmp_path, monkeypatch, division, expected
):
    class Empty:
        def close(self):
            pass

        def pages(self, source, params):
            assert source == "master"
            yield JQuantsPage(source, params, AT, b'{"data":[]}')

    store = setup(tmp_path, monkeypatch, Empty())
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")

    @contextmanager
    def lake(**_):
        yield None, SimpleNamespace(source=None)

    def frozen(*_, **__):
        from baibai_engine.data_collection.providers.jquants.contracts import (
            RetainedRequest,
            complete_request,
        )

        request = complete_request(
            (
                JQuantsPage(
                    "market_calendar",
                    {"from": DAY.isoformat(), "to": DAY.isoformat()},
                    AT,
                    json.dumps({"data": [{"Date": DAY.isoformat(), "HolDiv": division}]}).encode(),
                ),
            )
        )
        indexes = {
            source: index_market_originals(
                [RetainedRequest("calendar", request, None)] if source == "market_calendar" else [],
                source=source,
            )
            for source in (
                "daily_bars",
                "market_calendar",
                "margin_interest",
                "margin_alerts",
                "short_sale_reports",
            )
        }
        return planning.FixedMarketIndexes("fixed", "a" * 64, "partial", AT, indexes, (), ())

    monkeypatch.setattr(objects, "open_lake", lake)
    monkeypatch.setattr(readers, "resolve_current_release", lambda *_, **__: None)
    monkeypatch.setattr(planning, "freeze_market_originals", frozen)
    assert handoff._acquire_market_l1(DAY, tmp_path / "acquire") is expected
    fixed = MasterL1(store, tmp_path / "cold").fixed()
    assert next(fixed.requests(store, days=[DAY])).request.records() == []
    result = json.loads((tmp_path / "acquire/result.json").read_bytes())["independent_l1"]["master"]
    assert result["incomplete"] == 1
    if expected:
        assert result["status"] == "not_required_non_business_day"
