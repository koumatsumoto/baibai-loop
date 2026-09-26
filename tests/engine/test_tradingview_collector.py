from __future__ import annotations

import asyncio
import copy
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from baibai_engine.market.sqlite import open_connection
from baibai_engine.market.tradingview.collector import (
    SourceDataError,
    collect,
    preflight_snapshot,
    universe,
)
from baibai_engine.market.tradingview.observations import COLUMNS, FetchError, normalize_batch
from baibai_engine.market.universe import UniverseSourceDriftError

DAY = date(2026, 9, 18)
NOW = datetime(2026, 9, 18, 7, tzinfo=UTC)


def store(path: Path, count: int = 51) -> Path:
    conn = open_connection(path)
    with conn:
        conn.execute("INSERT INTO jquants_market_calendar VALUES (?,1)", (DAY.isoformat(),))
        conn.executemany(
            "INSERT INTO jquants_master_snapshots VALUES (?,?,?,'プライム','機械',1)",
            [(DAY.isoformat(), str(1000 + i), "IPO") for i in range(count)],
        )
    conn.close()
    return path


def payload(symbols: list[str], *, missing: list[str] | None = None) -> dict:
    missing = missing or []
    raw = dict.fromkeys(COLUMNS)
    raw.update(close=100, currency="JPY", earnings_per_share_forecast_next_fy=-5)
    return {
        "success": True,
        "data": {s: dict(raw) for s in symbols if s not in missing},
        "count": len(symbols) - len(missing),
        "missing": [{"symbol": s, "reason": "Quote data not found"} for s in missing],
        "missing_count": len(missing),
    }


def stored(path: Path) -> list:
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT * FROM tradingview_forecast_snapshots ORDER BY ticker"
        ).fetchall()


def test_full_universe_batches_and_same_day_immutability(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite")
    calls = []
    pauses = []

    async def fetch(symbols):
        calls.append(symbols)
        return payload(symbols, missing=symbols[1:2] if len(symbols) == 50 else [])

    async def sleep(interval):
        pauses.append(interval)

    result = asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW, sleep=sleep))
    assert [len(c) for c in calls] == [50, 1]
    assert pauses == [15]
    assert result["rows"] == 51
    assert result["unresolved"] == 1
    original = stored(path)
    result = asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW))
    assert result["status"] == "already_saved"
    assert len(calls) == 2
    assert stored(path) == original
    assert preflight_snapshot(path, DAY) == {
        "status": "already_saved",
        "snapshot_date": DAY.isoformat(),
        "rows": 51,
    }
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT fetch_status, eps_forecast_next_fy, fetched_at_utc, estimate_currency "
            "FROM tradingview_forecast_snapshots WHERE ticker='1000'"
        ).fetchone() == ("ok", -5.0, NOW.isoformat(), None)
        assert conn.execute(
            "SELECT fetch_status, recommendation_total FROM tradingview_forecast_snapshots "
            "WHERE ticker='1001'"
        ).fetchone() == ("unresolved", None)


def test_preflight_is_read_only_and_rejects_partial_rows(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    with pytest.raises(SourceDataError):
        preflight_snapshot(missing, DAY)
    assert not missing.exists()
    path = store(tmp_path / "market.sqlite", 2)
    assert preflight_snapshot(path, DAY) is None
    with sqlite3.connect(path) as conn:
        conn.execute(
            "DELETE FROM jquants_master_snapshots WHERE snapshot_date=?", (DAY.isoformat(),)
        )
    assert preflight_snapshot(path, DAY) is None  # Early slot fetches master next.
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT INTO jquants_master_snapshots VALUES (?,?,?,'プライム','機械',1)",
            [(DAY.isoformat(), str(1000 + i), "IPO") for i in range(2)],
        )

    async def healthy(symbols):
        return payload(symbols)

    asyncio.run(collect(path, DAY, healthy, interval=0, clock=lambda: NOW))
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM tradingview_forecast_snapshots WHERE ticker='1001'")
    with pytest.raises(SourceDataError, match="Partial"):
        preflight_snapshot(path, DAY)

    async def forbidden(_symbols):
        pytest.fail("partial snapshot must not reach provider")

    with pytest.raises(SourceDataError, match="Partial"):
        asyncio.run(collect(path, DAY, forbidden, clock=lambda: NOW))


@pytest.mark.parametrize("failure", ["429", "all_missing", "malformed", "crash"])
def test_failed_later_chunk_leaves_no_partial_snapshot(tmp_path: Path, failure: str) -> None:
    path = store(tmp_path / "market.sqlite")
    calls = 0

    async def fetch(symbols):
        nonlocal calls
        calls += 1
        if calls == 1:
            return payload(symbols)
        if failure == "all_missing":
            return payload(symbols, missing=symbols)
        if failure == "malformed":
            return {"success": True}
        raise RuntimeError(failure)

    with pytest.raises(RuntimeError):
        asyncio.run(collect(path, DAY, fetch, interval=0, clock=lambda: NOW))
    assert stored(path) == []

    # A same-day whole-run retry can still succeed.
    async def healthy(symbols):
        return payload(symbols)

    assert asyncio.run(collect(path, DAY, healthy, interval=0, clock=lambda: NOW))["rows"] == 51


@pytest.mark.parametrize(
    "observed", [NOW - timedelta(days=1), NOW + timedelta(days=1), NOW.replace(hour=5)]
)
def test_no_backfill_or_preclose_collection(tmp_path: Path, observed: datetime) -> None:
    path = store(tmp_path / "market.sqlite", 1)

    async def forbidden(symbols):
        pytest.fail("provider must not be called")

    with pytest.raises(FetchError, match="backfill"):
        asyncio.run(collect(path, DAY, forbidden, clock=lambda: observed))
    assert not stored(path)


def test_midnight_crossing_aborts_the_run(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    times = iter([NOW, NOW, NOW + timedelta(days=1)])

    async def fetch(symbols):
        return payload(symbols)

    with pytest.raises(FetchError):
        asyncio.run(collect(path, DAY, fetch, clock=lambda: next(times)))
    assert not stored(path)


def test_universe_excludes_secondary_instruments_without_bar_history(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    conn = open_connection(path)
    with conn:
        conn.executemany(
            "INSERT INTO jquants_master_snapshots VALUES (?,?,?, ?,?,1)",
            [
                (DAY.isoformat(), "2000", "fund", "プライム", "その他"),
                (DAY.isoformat(), "2001", "pro", "TOKYO PRO MARKET", "機械"),
                (DAY.isoformat(), "2002", "Nagoya", "名証", "機械"),
            ],
        )
    assert universe(conn, DAY) == ["TSE:1000"]
    with conn:
        conn.execute("UPDATE jquants_master_snapshots SET sector_33='unknown' WHERE ticker='1000'")
    with pytest.raises(UniverseSourceDriftError):
        universe(conn, DAY)
    conn.close()


def test_exact_date_master_and_calendar_are_required(tmp_path: Path) -> None:
    path = store(tmp_path / "market.sqlite", 1)
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM jquants_master_snapshots")

    async def forbidden(symbols):
        pytest.fail("provider must not be called")

    with pytest.raises(FetchError, match="Exact-date"):
        asyncio.run(collect(path, DAY, forbidden, clock=lambda: NOW))


def test_normal_null_is_not_unresolved() -> None:
    result = normalize_batch(payload(["TSE:1000"]), ["TSE:1000"], NOW)
    assert result[0]["fetch_status"] == "ok"
    assert result[0]["recommendation_total"] is None


@pytest.mark.parametrize(
    "case", ["unknown", "overlap", "duplicate_missing", "count", "missing_field", "nan"]
)
def test_response_accounting_refuses_invalid_payload(case: str) -> None:
    symbols = ["TSE:1000", "TSE:1001"]
    p = copy.deepcopy(payload(symbols, missing=[symbols[1]]))
    if case == "unknown":
        p["data"]["NAG:1000"] = p["data"].pop(symbols[0])
    elif case == "overlap":
        p["missing"][0]["symbol"] = symbols[0]
    elif case == "duplicate_missing":
        p["missing"].append(p["missing"][0])
    elif case == "count":
        p["count"] = 99
    elif case == "missing_field":
        p["data"][symbols[0]].pop("close")
    else:
        p["data"][symbols[0]]["close"] = float("nan")
    with pytest.raises(FetchError):
        normalize_batch(p, symbols, NOW)


@pytest.mark.parametrize("common", [0, None])
def test_universe_uses_canonical_sectors_without_common_stock_flag(tmp_path, common):
    path = store(tmp_path / "market.sqlite", 1)
    conn = open_connection(path)
    with conn:
        conn.execute("UPDATE jquants_master_snapshots SET is_common_stock=?", (common,))
    assert universe(conn, DAY) == ["TSE:1000"]
    conn.close()


class Timer:
    value = 0.0

    def __call__(self):
        return self.value


@pytest.mark.parametrize(
    "failure", [None, "429", "all_missing", "malformed", "store", "time_guard"]
)
def test_progress_and_durations_without_partial_writes(tmp_path, failure):
    from dataclasses import asdict

    from baibai_engine.market.tradingview.cli import failure_category
    from baibai_engine.market.tradingview.collector import CollectionFailure
    from baibai_engine.market.tradingview.observations import (
        ProviderHTTPError,
        ProviderPayloadError,
    )

    path = store(tmp_path / "market.sqlite")
    if failure == "store":
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TRIGGER fail_insert BEFORE INSERT ON tradingview_forecast_snapshots "
                "WHEN NEW.ticker='1050' BEGIN SELECT RAISE(ABORT, 'private-token'); END"
            )
    timer = Timer()
    events = []
    calls = 0
    pauses = []

    async def fetch(symbols):
        nonlocal calls
        calls += 1
        timer.value += calls * 1.23456
        if calls == 2:
            if failure == "429":
                raise ProviderHTTPError(429)
            if failure == "all_missing":
                return payload(symbols, missing=symbols)
            if failure == "malformed":
                return {"success": True}
        return payload(symbols)

    async def sleep(interval):
        pauses.append(interval)
        timer.value += interval

    def clock():
        return NOW + timedelta(
            days=1 if failure == "time_guard" and calls == 2 else 0, seconds=timer.value
        )

    async def run():
        return await collect(
            path, DAY, fetch, clock=clock, timer=timer, sleep=sleep, progress=events.append
        )

    if failure:
        with pytest.raises(CollectionFailure) as caught:
            asyncio.run(run())
        error = caught.value
        expected_category = {
            "429": "provider_rate_limit",
            "all_missing": "provider_all_missing",
            "malformed": "provider_response",
            "store": "storage",
            "time_guard": "time_guard",
        }
        assert failure_category(error) == expected_category[failure]
        assert error.progress == events[-1]
        assert events[-1].phase == (
            "store" if failure == "store" else "fetch" if failure == "429" else "normalize"
        )
        assert events[-1].chunks_completed == (2 if failure == "store" else 1)
        if failure != "store":
            assert events[-1].chunk_index == 2
            assert events[-1].chunk_size == 1
        if failure == "malformed":
            assert isinstance(error.cause, ProviderPayloadError)
            assert error.cause.reason == "malformed_envelope"
        assert not stored(path)
    else:
        result = asyncio.run(run())
        assert result["rows"] == 51
        assert result["unresolved"] == 0
        assert result["chunks_total"] == result["chunks_completed"] == 2
        assert result["columns_count"] == len(COLUMNS)
        assert result["interval_seconds"] == 15
        assert result["first_fetched_at_utc"] == (NOW + timedelta(seconds=1.23456)).isoformat()
        assert result["last_fetched_at_utc"] == (NOW + timedelta(seconds=18.70368)).isoformat()
        assert [e.phase for e in events] == [
            "prepare",
            "fetch",
            "normalize",
            "between_chunks",
            "fetch",
            "normalize",
            "between_chunks",
            "store",
            "complete",
        ]
    assert calls == 2
    assert pauses == [15]
    assert events[-1].provider_elapsed_seconds == 3.704
    assert events[-1].max_chunk_elapsed_seconds == 2.469
    assert events[-1].elapsed_seconds == 18.704
    assert events[-1].response_bytes > 0
    assert events[-1].expected_universe == 51
    assert events[-1].chunks_total == 2
    assert "TSE:" not in repr([asdict(e) for e in events])


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("quote_close", float("nan"), "invalid_numeric"),
        ("recommendation_total", -1, "invalid_analyst_count"),
        ("quote_currency", 42, "invalid_text_type"),
    ],
)
def test_payload_diagnostics_use_canonical_field(field, value, reason):
    from baibai_engine.market.tradingview.observations import FIELDS, ProviderPayloadError

    data = payload(["TSE:1000"])
    data["data"]["TSE:1000"][FIELDS[field]] = value
    with pytest.raises(ProviderPayloadError) as caught:
        normalize_batch(data, ["TSE:1000"], NOW)
    assert (caught.value.reason, caught.value.field) == (reason, field)


def test_missing_fields_uses_first_requested_canonical_field():
    from baibai_engine.market.tradingview.observations import ProviderPayloadError

    data = payload(["TSE:1000"])
    del data["data"]["TSE:1000"]["earnings_per_share_forecast_next_fy"]
    del data["data"]["TSE:1000"]["close"]
    with pytest.raises(ProviderPayloadError) as caught:
        normalize_batch(data, ["TSE:1000"], NOW)
    assert caught.value.field == "eps_forecast_next_fy"


@pytest.mark.parametrize("raw", [None, [], "private-body", 42])
def test_non_dict_row_does_not_blame_a_field(raw):
    from baibai_engine.market.tradingview.cli import failure_line
    from baibai_engine.market.tradingview.observations import ProviderPayloadError

    data = payload(["TSE:1000"])
    data["data"]["TSE:1000"] = raw
    with pytest.raises(ProviderPayloadError) as caught:
        normalize_batch(data, ["TSE:1000"], NOW)
    assert caught.value.reason == "row_missing_requested_fields"
    assert caught.value.field is None
    assert "validation_field=-" in failure_line(caught.value)
    assert "private-body" not in failure_line(caught.value)
