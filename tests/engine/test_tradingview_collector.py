from __future__ import annotations

import asyncio
import copy
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from baibai_engine.market.sqlite import open_connection
from baibai_engine.market.tradingview.collector import collect, universe
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
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT fetch_status, eps_forecast_next_fy, fetched_at_utc, estimate_currency "
            "FROM tradingview_forecast_snapshots WHERE ticker='1000'"
        ).fetchone() == ("ok", -5.0, NOW.isoformat(), None)
        assert conn.execute(
            "SELECT fetch_status, recommendation_total FROM tradingview_forecast_snapshots "
            "WHERE ticker='1001'"
        ).fetchone() == ("unresolved", None)


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
