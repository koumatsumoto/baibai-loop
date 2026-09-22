"""Serial full-universe fetch followed by one append-only SQLite transaction."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from datetime import time as daytime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from baibai_engine.market.sqlite import open_connection
from baibai_engine.market.universe import (
    ELIGIBLE_MARKETS,
    NON_COMMON_STOCK_SECTORS,
    TSE_33_SECTORS,
    UniverseSourceDriftError,
)

from .contract import COLUMNS, TABLE
from .observations import FIELDS, FetchError, normalize_batch

JST = ZoneInfo("Asia/Tokyo")
BatchFetch = Callable[[list[str]], Awaitable[dict[str, Any]]]


def universe(conn: sqlite3.Connection, day: date) -> list[str]:
    rows = conn.execute(
        "SELECT ticker,market,sector_33,is_common_stock FROM jquants_master_snapshots "
        "WHERE snapshot_date=? ORDER BY ticker",
        (day.isoformat(),),
    ).fetchall()
    if not rows:
        raise FetchError("Exact-date J-Quants master is unavailable")
    result = []
    for ticker, market, sector, common in rows:
        if str(market).upper() not in ELIGIBLE_MARKETS:
            continue
        if sector not in TSE_33_SECTORS | NON_COMMON_STOCK_SECTORS:
            raise UniverseSourceDriftError("Unknown J-Quants sector classification")
        if sector not in TSE_33_SECTORS or not common:
            continue
        if not re.fullmatch(r"[0-9][0-9A-Z]{3}", ticker):
            raise FetchError("Invalid J-Quants ticker")
        result.append("TSE:" + ticker)
    if not result:
        raise FetchError("Empty TradingView universe")
    return result


def validate_time(day: date, now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("Timezone-aware observation clock required")
    local = now.astimezone(JST)
    if local.date() != day or local.time() < daytime(15, 30):
        raise FetchError("Snapshots require today's post-close observation; backfill is forbidden")


async def collect(
    path: Path,
    day: date,
    fetch: BatchFetch,
    *,
    interval: float = 15.0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, object]:
    if not 0 <= interval <= 3600:
        raise ValueError("interval must be between 0 and 3600 seconds")
    validate_time(day, clock())
    conn = open_connection(path)
    started = time.monotonic()
    try:
        existing = conn.execute(
            "SELECT COUNT(*) FROM tradingview_forecast_snapshots WHERE snapshot_date=?",
            (day.isoformat(),),
        ).fetchone()[0]
        if existing:
            return {"status": "already_saved", "snapshot_date": day.isoformat(), "rows": existing}
        calendar = conn.execute(
            "SELECT is_business_day FROM jquants_market_calendar WHERE day=?", (day.isoformat(),)
        ).fetchone()
        if calendar is None:
            raise FetchError("Exact-date trading calendar is unavailable")
        if not calendar[0]:
            return {"status": "non_trading_day", "snapshot_date": day.isoformat()}
        symbols = universe(conn, day)
        rows: list[dict[str, Any]] = []
        response_bytes = 0
        for offset in range(0, len(symbols), 50):
            if offset:
                await sleep(interval)
            validate_time(day, clock())
            chunk = symbols[offset : offset + 50]
            payload = await fetch(chunk)
            fetched = clock()
            validate_time(day, fetched)
            response_bytes += len(json.dumps(payload, ensure_ascii=False).encode())
            rows.extend(normalize_batch(payload, chunk, fetched))
        # All chunks have been accounted for; a failure above writes no canonical rows.
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT COUNT(*) FROM tradingview_forecast_snapshots WHERE snapshot_date=?",
            (day.isoformat(),),
        ).fetchone()[0]
        if existing:
            conn.rollback()
            return {"status": "already_saved", "snapshot_date": day.isoformat(), "rows": existing}
        names = [column[0] for column in COLUMNS]
        sql = f"INSERT INTO {TABLE} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})"  # nosec B608
        conn.executemany(
            sql,
            [
                tuple(
                    day.isoformat() if name == "snapshot_date" else row.get(name) for name in names
                )
                for row in rows
            ],
        )
        conn.commit()
        return {
            "status": "saved",
            "snapshot_date": day.isoformat(),
            "expected_universe": len(symbols),
            "rows": len(rows),
            "unresolved": sum(row["fetch_status"] == "unresolved" for row in rows),
            "response_bytes": response_bytes,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "normal_nulls": {
                name: sum(row["fetch_status"] == "ok" and row[name] is None for row in rows)
                for name in FIELDS
            },
        }
    finally:
        conn.close()
