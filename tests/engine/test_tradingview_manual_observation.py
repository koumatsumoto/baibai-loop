from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import httpx2
import pytest
from tests.engine.test_tradingview_collector import Timer, _sdk, payload, store, stored

from baibai_engine.data_collection.tradingview.collector import collect, preflight_snapshot
from baibai_engine.data_collection.tradingview import cli
from baibai_engine.machine_analysis.external_assessment.tradingview.diagnostics import (
    SourceDataError,
    TimeGuardError,
)
from baibai_engine.machine_analysis.external_assessment.tradingview.observations import FetchError

DAY = date(2026, 10, 11)
MASTER = date(2026, 10, 9)
NOW = datetime(2026, 10, 11, 1, tzinfo=UTC)


def manual_store(tmp_path, count=153):
    path = store(tmp_path / "market.sqlite", count)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE jquants_master_snapshots SET snapshot_date=?, record_json=json_set(record_json, '$.Date', ?)",
            (MASTER.isoformat(), MASTER.isoformat()),
        )
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,0)", (DAY.isoformat(),))
        conn.execute("INSERT INTO jquants_market_calendar VALUES ('2026-10-10',0)")
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,1)", (MASTER.isoformat(),))
        conn.executemany(
            "INSERT INTO tradingview_forecast_snapshots(snapshot_date,ticker,provider_symbol,fetched_at_utc,fetch_status,quote_close) VALUES (?,?,?,?,?,?)",
            [
                (DAY.isoformat(), str(1000 + i), f"TSE:{1000 + i}", NOW.isoformat(), "ok", 123)
                for i in range(100)
            ],
        )
    return path


def test_weekend_observation_resumes_only_missing_and_preserves_nulls_times_and_master(tmp_path):
    path = manual_store(tmp_path)
    prior = stored(path)
    assert preflight_snapshot(path, DAY)["status"] == "needs_fetch"
    before = path.read_bytes()
    state = preflight_snapshot(path, DAY, master_asof=MASTER)
    assert path.read_bytes() == before
    assert (state["rows"], state["remaining"], state["master_asof"]) == (100, 53, "2026-10-09")
    timer = Timer()
    starts, ends = [], []
    requests = []

    async def fetch(symbols, _columns):
        requests.append(symbols)
        starts.append(timer.value)
        timer.value += 5
        ends.append(timer.value)
        return _sdk(payload(symbols))

    async def sleep(seconds):
        assert seconds >= 15
        timer.value += seconds

    result = asyncio.run(
        collect(
            path,
            DAY,
            fetch,
            clock=lambda: NOW + timedelta(seconds=timer.value),
            timer=timer,
            sleep=sleep,
        )
    )
    assert requests == [
        [f"TSE:{i}" for i in range(1100, 1150)],
        [f"TSE:{i}" for i in range(1150, 1153)],
    ]
    assert starts[1] - ends[0] == 15
    assert (result["status"], result["rows_added"], result["remaining"]) == ("saved", 53, 0)
    assert result["master_asof"] == MASTER.isoformat()
    assert result["snapshot_date"] == DAY.isoformat()
    assert stored(path)[:100] == prior
    assert result["normal_nulls"]["analyst_rating"] == 153
    with closing(sqlite3.connect(path)) as conn, conn:
        assert conn.execute(
            "SELECT DISTINCT snapshot_date FROM jquants_master_snapshots"
        ).fetchall() == [(MASTER.isoformat(),)]
        assert conn.execute(
            "SELECT snapshot_date,fetched_at_utc,quote_at_utc,provider_updated_at_utc FROM tradingview_forecast_snapshots WHERE ticker='1100'"
        ).fetchone() == (DAY.isoformat(), (NOW + timedelta(seconds=5)).isoformat(), None, None)
    before = path.read_bytes()
    again = asyncio.run(collect(path, DAY, fetch, master_asof=MASTER, clock=lambda: NOW))
    assert again["status"] == "already_saved"
    assert len(requests) == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("failure", ["429", "day_crossing", "soft_budget"])
def test_manual_partial_preserves_commits_and_resumes_same_day(tmp_path, failure):
    path = manual_store(tmp_path, 152)
    prior = stored(path)
    timer = Timer()
    calls = []

    async def fetch(symbols, _columns):
        calls.append(symbols)
        if failure == "soft_budget":
            timer.value = 1490
        if len(calls) == 2 and failure == "429":
            response = httpx2.Response(
                429,
                headers={"Retry-After": "60"},
                request=httpx2.Request("GET", "https://example.com"),
            )
            raise httpx2.HTTPStatusError("private", request=response.request, response=response)
        return _sdk(payload(symbols))

    async def sleep(_seconds):
        pass

    def clock():
        return NOW + timedelta(days=int(failure == "day_crossing" and len(calls) == 2))

    result = asyncio.run(
        collect(path, DAY, fetch, master_asof=MASTER, clock=clock, timer=timer, sleep=sleep)
    )
    assert (result["status"], result["rows_added"], result["remaining"]) == ("partial", 50, 2)
    assert (
        result["stop_reason"]
        == {
            "429": "provider_rate_limit",
            "day_crossing": "time_guard",
            "soft_budget": "soft_budget",
        }[failure]
    )
    if failure == "429":
        assert "retry_not_before_utc=" in result["last_failure"]
        assert "private" not in result["last_failure"]
    first = stored(path)
    assert first[:100] == prior
    requests = []

    async def resume(symbols, _columns):
        requests.append(symbols)
        return _sdk(payload(symbols))

    result = asyncio.run(collect(path, DAY, resume, master_asof=MASTER, clock=lambda: NOW))
    assert (result["rows_added"], result["remaining"]) == (2, 0)
    assert requests == [["TSE:1150", "TSE:1151"]]
    assert stored(path)[:150] == first


@pytest.mark.parametrize(
    "case",
    [
        "past_observation",
        "future_observation",
        "future_master",
        "missing_master",
        "short_interval",
    ],
)
def test_manual_cli_rejects_invalid_mode_before_oauth(tmp_path, monkeypatch, capsys, case):
    path = manual_store(tmp_path)

    class Clock:
        @staticmethod
        def now(tz):
            return NOW.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(
        cli, "credential_storage", lambda *_: pytest.fail("invalid observation must not load OAuth")
    )
    args = ["refresh", "--sqlite", str(path), "--master-asof", MASTER.isoformat()]
    if case == "past_observation":
        args += ["--asof", "2026-10-09"]
    if case == "future_observation":
        args += ["--asof", "2026-10-12"]
    if case == "future_master":
        args[-1] = "2026-10-12"
    if case == "missing_master":
        args[-1] = "2026-10-08"
    if case == "short_interval":
        args += ["--interval", "14"]
    before = path.read_bytes()
    assert cli.main(args) == 1
    output = capsys.readouterr()
    assert "private" not in output.err
    assert path.read_bytes() == before


def test_manual_cli_passes_today_and_saved_master_and_completed_noop_avoids_oauth(
    tmp_path, monkeypatch, capsys
):
    path = manual_store(tmp_path, 101)

    class Clock:
        @staticmethod
        def now(tz):
            return NOW.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(cli, "credential_storage", lambda *_: object())

    async def snapshot(_storage, actual_path, observation_date, interval, _progress, **options):
        assert actual_path == path
        assert observation_date == DAY
        assert options["master_asof"] == MASTER
        assert interval == 15

        async def fetch(symbols, _columns):
            return _sdk(payload(symbols))

        return await collect(path, DAY, fetch, master_asof=MASTER, clock=lambda: NOW)

    monkeypatch.setattr(cli, "snapshot", snapshot)
    args = ["refresh", "--sqlite", str(path), "--master-asof", MASTER.isoformat()]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["rows_added"] == 1
    assert result["master_asof"] == MASTER.isoformat()
    monkeypatch.setattr(
        cli,
        "credential_storage",
        lambda *_: pytest.fail("completed observation must not load OAuth"),
    )
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_saved"


def test_weekend_response_after_midnight_is_rejected(tmp_path):
    path = manual_store(tmp_path, 101)

    before = stored(path)
    count = 0

    async def fetch(symbols, _columns):
        nonlocal count
        count += 1
        return _sdk(payload(symbols))

    with pytest.raises(FetchError):
        asyncio.run(
            collect(path, DAY, fetch, master_asof=MASTER, clock=lambda: NOW + timedelta(days=count))
        )
    assert stored(path) == before


@pytest.mark.parametrize("observation_day", [DAY, date(2026, 10, 12)])
def test_rest_day_automatically_uses_calendar_business_day_and_observation_hours(
    tmp_path, observation_day
):
    path = manual_store(tmp_path, 101)
    with closing(sqlite3.connect(path)) as conn, conn:
        if observation_day != DAY:
            conn.execute(
                "INSERT INTO jquants_market_calendar VALUES (?,0)", (observation_day.isoformat(),)
            )
            conn.execute(
                "UPDATE tradingview_forecast_snapshots SET snapshot_date=?",
                (observation_day.isoformat(),),
            )
    before = stored(path)
    morning = datetime.combine(observation_day, datetime.min.time(), UTC)
    assert (
        preflight_snapshot(path, observation_day, now=morning - timedelta(seconds=1))["status"]
        == "before_observation"
    )
    calls = []

    async def fetch(symbols, _columns):
        calls.append(symbols)
        return _sdk(payload(symbols))

    result = asyncio.run(collect(path, observation_day, fetch, clock=lambda: morning))
    assert (result["status"], result["master_asof"], result["rows_added"]) == (
        "saved",
        MASTER.isoformat(),
        1,
    )
    assert calls == [["TSE:1100"]]
    assert stored(path)[:100] == before
    result = asyncio.run(collect(path, observation_day, fetch, clock=lambda: morning))
    assert result["requests_attempted"] == 0
    assert len(calls) == 1


def test_calendar_required_master_missing_does_not_use_older_saved_master(tmp_path):
    path = manual_store(tmp_path, 101)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE jquants_master_snapshots SET snapshot_date='2026-09-18', record_json=json_set(record_json,'$.Date','2026-09-18')"
        )
    before = path.read_bytes()
    with pytest.raises(SourceDataError, match="calendar-required exact-date master"):
        preflight_snapshot(path, DAY, now=NOW)
    with pytest.raises(SourceDataError, match="differs from calendar"):
        preflight_snapshot(path, DAY, master_asof=date(2026, 9, 18), now=NOW)
    assert path.read_bytes() == before


@pytest.mark.parametrize("missing_day", ["2026-10-09", "2026-10-10"])
def test_calendar_gap_cannot_select_an_older_master_or_collect(tmp_path, missing_day):
    path = manual_store(tmp_path, 101)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO jquants_market_calendar VALUES ('2026-10-08',1)")
        conn.execute(
            "UPDATE jquants_master_snapshots SET snapshot_date='2026-10-08', record_json=json_set(record_json,'$.Date','2026-10-08')"
        )
        conn.execute("DELETE FROM jquants_market_calendar WHERE day=?", (missing_day,))
    before = path.read_bytes()
    with pytest.raises(SourceDataError, match="Continuous trading calendar coverage"):
        preflight_snapshot(path, DAY, now=NOW)

    async def forbidden(_symbols):
        pytest.fail("calendar gap must stop before provider access")

    with pytest.raises(SourceDataError, match="Continuous trading calendar coverage"):
        asyncio.run(collect(path, DAY, forbidden, clock=lambda: NOW))
    assert path.read_bytes() == before


def test_partial_preflight_keeps_quality_when_cooldown_prevents_fetch(tmp_path):
    path = manual_store(tmp_path, 101)
    last_received = (NOW + timedelta(seconds=1)).isoformat()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE tradingview_forecast_snapshots SET fetch_status='unresolved' WHERE ticker='1000'"
        )
        conn.execute(
            "UPDATE tradingview_forecast_snapshots SET fetched_at_utc=? WHERE ticker='1001'",
            (last_received,),
        )
    before = path.read_bytes()
    result = preflight_snapshot(path, DAY, now=NOW + timedelta(seconds=2))
    assert result["status"] == "needs_fetch"
    assert (result["rows"], result["remaining"], result["unresolved"]) == (100, 1, 1)
    assert result["normal_nulls"]["price_target_average"] == 99
    assert result["normal_nulls"]["quote_close"] == 0
    assert result["first_fetched_at_utc"] == NOW.isoformat()
    assert result["last_fetched_at_utc"] == last_received
    assert path.read_bytes() == before


def test_business_day_manual_morning_does_not_block_post_close_collection(
    tmp_path, monkeypatch, capsys
):
    day = date(2026, 9, 18)
    path = store(tmp_path / "market.sqlite", 1)
    morning = datetime(2026, 9, 18, 1, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz):
            return morning.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", Clock)
    monkeypatch.setattr(
        cli, "credential_storage", lambda *_: pytest.fail("before-close must not load OAuth")
    )
    assert cli.main(["refresh", "--sqlite", str(path), "--master-asof", day.isoformat()]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "before_close"
    assert stored(path) == []

    async def fetch(symbols, _columns):
        return _sdk(payload(symbols))

    with pytest.raises(TimeGuardError):
        asyncio.run(collect(path, day, fetch, master_asof=day, clock=lambda: morning))
    result = asyncio.run(
        collect(path, day, fetch, master_asof=day, clock=lambda: morning + timedelta(hours=6))
    )
    assert result["status"] == "saved"


def test_batch_size_option_is_removed_before_any_credential_access(monkeypatch):
    monkeypatch.setattr(cli, "credential_storage", lambda *_: pytest.fail("must not read OAuth"))
    with pytest.raises(SystemExit) as error:
        cli.main(["refresh", "--batch-size", "50"])
    assert error.value.code == 2
