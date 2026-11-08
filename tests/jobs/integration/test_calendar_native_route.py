"""Independent calendar is committed before release-dependent market acquisition."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from tests.engine.data_collection.providers.jquants.test_calendar_native import AT, DAY
from tests.helpers.r2_store import MemoryR2Store

import baibai_engine.data_collection.providers.jquants.http as http
import baibai_engine.storage.snapshots.objects as objects
import baibai_jobs.integration.source_handoff as handoff
from baibai_engine.data_collection.providers.jquants.calendar_native import CalendarL1, interval
from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
    JQuantsRateLimitError,
)


def setup(tmp_path, monkeypatch, client):
    store = MemoryR2Store()
    store.close = lambda: None
    CalendarL1(store, tmp_path / "initialize").initialize()
    monkeypatch.setenv("BAIBAI_CALENDAR_NATIVE", "false")
    from baibai_jobs.acquisition import (
        jquants_margin_alerts_native,
        jquants_margin_interest_native,
        jquants_master_native,
        jquants_price_native,
        jquants_short_sale_reports_native,
    )

    for module, method in (
        (jquants_master_native, "acquire_master_native"),
        (jquants_price_native, "acquire_price_native"),
        (jquants_margin_alerts_native, "acquire_margin_alerts_native"),
        (jquants_short_sale_reports_native, "acquire_short_sale_reports_native"),
        (jquants_margin_interest_native, "acquire_margin_interest_native"),
    ):
        monkeypatch.setattr(
            module,
            method,
            lambda *a, **kw: {
                "failed": 0,
                "held": 0,
                "incomplete": 0,
                "remaining": {"unknown_calendar_days": [], "unknown_incomplete_requests": []},
            },
        )
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
    assert handoff._acquire_market_l1(DAY, tmp_path / "acquire")
    fixed = CalendarL1(store, tmp_path / "cold").fixed()
    assert fixed.plan(start=DAY - timedelta(days=7), end=DAY + timedelta(days=45)) == ()
    assert len(fixed.value()["days"]) == 53
    assert (
        json.loads((tmp_path / "acquire/calendar-native-result.json").read_text())["changed"] == 1
    )


def test_final_calendar_quota_halts_price_master_and_remaining_market_gets(tmp_path, monkeypatch):
    client = Client(JQuantsRateLimitError("quota"))
    setup(tmp_path, monkeypatch, client)
    assert not handoff._acquire_market_l1(DAY, tmp_path / "acquire")
    report = json.loads((tmp_path / "acquire/result.json").read_text())
    assert all(value.get("halted") for value in report["independent_l1"].values())
    assert len(client.calls) == 1


def test_old_bootstrap_calendar_writer_cannot_run_after_native_cutover(monkeypatch):
    from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
    from baibai_jobs.maintenance.source_refresh.jquants import JQuantsRefresh

    monkeypatch.setenv("BAIBAI_CALENDAR_NATIVE", "false")
    with pytest.raises(JQuantsProviderError, match="jquants-calendar-native"):
        JQuantsRefresh.ensure_market_calendar(SimpleNamespace(), DAY, DAY)
