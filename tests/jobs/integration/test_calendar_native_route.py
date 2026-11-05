"""Independent calendar is committed before release-dependent market acquisition."""

import json
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest
from tests.engine.data_collection.providers.jquants.test_calendar_native import AT, DAY
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.providers.jquants.http as http
import baibai_engine.storage.snapshots.objects as objects
import baibai_engine.storage.snapshots.reader as readers
import baibai_jobs.acquisition.jquants_market_plan as planning
import baibai_jobs.acquisition.jquants_market_receive as receive
import baibai_jobs.integration.source_handoff as handoff
from baibai_engine.data_collection.providers.jquants.calendar_native import CalendarL1, interval
from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    JQuantsRateLimitError,
)
from baibai_engine.data_collection.providers.jquants.native_index import (
    calendar_sessions,
    index_market_originals,
)
from baibai_jobs.calendar_cutover import calendar_native_enabled


def setup(tmp_path, monkeypatch, client):
    store = MemoryR2Store()
    store.close = lambda: None
    CalendarL1(store, tmp_path / "initialize").initialize()
    monkeypatch.setenv("BAIBAI_CALENDAR_NATIVE", "true")
    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "false")
    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "false")
    monkeypatch.setattr(handoff, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(http, "JQuantsHTTP", lambda *_: client)
    return store


class Client:
    def __init__(self, failure=None):
        self.failure, self.calls = failure, []

    def close(self):
        pass

    def pages(self, source, params):
        from datetime import date

        self.calls.append((source, params))
        assert source == "market_calendar"
        if self.failure:
            raise self.failure
        yield JQuantsPage(
            source,
            params,
            AT,
            json.dumps(
                {
                    "data": [
                        {"Date": day.isoformat(), "HolDiv": "0"}
                        for day in interval(
                            date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
                        )
                    ]
                }
            ).encode(),
        )


def test_calendar_commit_is_not_dependent_on_old_release_or_l2(tmp_path, monkeypatch):
    store = setup(tmp_path, monkeypatch, Client())
    monkeypatch.setattr(
        objects, "open_lake", lambda **_: (_ for _ in ()).throw(ValueError("old release absent"))
    )
    with pytest.raises(ValueError, match="old release absent"):
        handoff._acquire_market_l1(DAY, tmp_path / "acquire")
    fixed = CalendarL1(store, tmp_path / "cold").fixed()
    assert fixed.plan(start=DAY - timedelta(days=7), end=DAY + timedelta(days=45)) == ()
    assert len(fixed.value()["days"]) == 53
    assert (
        json.loads((tmp_path / "acquire/calendar-native-result.json").read_text())["changed"] == 1
    )


def fake_legacy(monkeypatch, captured):
    @contextmanager
    def lake(**_):
        yield None, SimpleNamespace(source=None)

    def frozen(*_, **kwargs):
        assert kwargs["include_calendar"] is False
        indexes = {
            source: index_market_originals([], source=source)
            for source in (
                "master",
                "daily_bars",
                "margin_interest",
                "margin_alerts",
                "short_sale_reports",
            )
        }
        return planning.FixedMarketIndexes("fixed", "a" * 64, "partial", AT, indexes, (), ())

    def legacy(directory, fixed, **kwargs):
        captured.update(fixed=fixed, options=kwargs)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(
            json.dumps({"acquisition": {"status": "skipped_non_business_day"}, "request_ids": []})
        )
        return not kwargs["provider_halted"]

    monkeypatch.setattr(objects, "open_lake", lake)
    monkeypatch.setattr(readers, "resolve_current_release", lambda *_, **__: None)
    monkeypatch.setattr(planning, "freeze_market_originals", frozen)
    monkeypatch.setattr(receive, "receive_market_handoff", legacy)


def test_remaining_margin_and_scheduled_plan_receive_new_native_calendar(tmp_path, monkeypatch):
    client = Client()
    setup(tmp_path, monkeypatch, client)
    captured = {}
    fake_legacy(monkeypatch, captured)
    assert handoff._acquire_market_l1(DAY, tmp_path / "acquire")
    assert captured["options"]["include_calendar"] is False
    assert captured["options"]["include_master"] is True
    assert captured["options"]["include_daily_prices"] is True
    sessions, unknown = calendar_sessions(
        captured["fixed"].indexes["market_calendar"], start=DAY, end=DAY
    )
    assert sessions[DAY] is False
    assert not unknown
    assert len(client.calls) == 1


def test_final_calendar_quota_halts_price_master_and_remaining_market_gets(tmp_path, monkeypatch):
    client = Client(JQuantsRateLimitError("quota"))
    setup(tmp_path, monkeypatch, client)
    monkeypatch.setenv("BAIBAI_MASTER_NATIVE", "true")
    monkeypatch.setenv("BAIBAI_PRICE_NATIVE", "true")
    captured = {}
    fake_legacy(monkeypatch, captured)
    assert not handoff._acquire_market_l1(DAY, tmp_path / "acquire")
    assert captured["options"]["provider_halted"] is True
    assert len(client.calls) == 1


@pytest.mark.parametrize("value", ["TRUE", "1", "yes", " true"])
def test_flag_is_strict_and_default_native(monkeypatch, value):
    monkeypatch.delenv("BAIBAI_CALENDAR_NATIVE", raising=False)
    assert calendar_native_enabled()
    monkeypatch.setenv("BAIBAI_CALENDAR_NATIVE", value)
    with pytest.raises(ValueError, match="true or false"):
        calendar_native_enabled()


def test_old_bootstrap_calendar_writer_cannot_run_after_native_cutover(monkeypatch):
    from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
    from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh

    monkeypatch.setenv("BAIBAI_CALENDAR_NATIVE", "true")
    with pytest.raises(JQuantsProviderError, match="jquants-calendar-native"):
        JQuantsRefresh.ensure_market_calendar(SimpleNamespace(), DAY, DAY)
