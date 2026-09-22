"""Serial full-universe fetch followed by one append-only SQLite transaction."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime
from datetime import time as daytime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from baibai_engine.market.sqlite import open_connection
from baibai_engine.market.universe import (
    ELIGIBLE_MARKETS,
    NON_COMMON_STOCK_SECTORS,
    TSE_33_SECTORS,
    UniverseSourceDriftError,
)

from .contract import COLUMNS, TABLE
from .observations import COLUMNS as REQUEST_COLUMNS
from .observations import FIELDS, FetchError, normalize_batch

JST = ZoneInfo("Asia/Tokyo")
BatchFetch = Callable[[list[str]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class CollectionProgress:
    snapshot_date: str
    phase: Literal["prepare", "fetch", "normalize", "between_chunks", "store", "complete"]
    expected_universe: int
    chunks_total: int
    chunks_completed: int = 0
    chunk_index: int | None = None
    chunk_size: int | None = None
    columns_count: int = len(REQUEST_COLUMNS)
    interval_seconds: float = 15.0
    response_bytes: int = 0
    provider_elapsed_seconds: float = 0.0
    max_chunk_elapsed_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    first_fetched_at_utc: str | None = None
    last_fetched_at_utc: str | None = None


ProgressHook = Callable[[CollectionProgress], None]


class CollectionFailure(FetchError):
    def __init__(self, cause: BaseException, progress: CollectionProgress) -> None:
        self.cause = cause
        self.progress = progress
        super().__init__("TradingView collection failed")


class TimeGuardError(FetchError):
    """Observation is outside the allowed date or close time."""


class SourceDataError(FetchError):
    """Required local source data is unavailable or invalid."""


def universe(conn: sqlite3.Connection, day: date) -> list[str]:
    rows = conn.execute(
        "SELECT ticker,market,sector_33 FROM jquants_master_snapshots "
        "WHERE snapshot_date=? ORDER BY ticker",
        (day.isoformat(),),
    ).fetchall()
    if not rows:
        raise SourceDataError("Exact-date J-Quants master is unavailable")
    result = []
    for ticker, market, sector in rows:
        if str(market).upper() not in ELIGIBLE_MARKETS:
            continue
        if sector not in TSE_33_SECTORS | NON_COMMON_STOCK_SECTORS:
            raise UniverseSourceDriftError("Unknown J-Quants sector classification")
        if sector not in TSE_33_SECTORS:
            continue
        if not re.fullmatch(r"[0-9][0-9A-Z]{3}", ticker):
            raise SourceDataError("Invalid J-Quants ticker")
        result.append("TSE:" + ticker)
    if not result:
        raise SourceDataError("Empty TradingView universe")
    return result


def validate_time(day: date, now: datetime) -> None:
    if now.tzinfo is None:
        raise ValueError("Timezone-aware observation clock required")
    local = now.astimezone(JST)
    if local.date() != day or local.time() < daytime(15, 30):
        raise TimeGuardError(
            "Snapshots require today's post-close observation; backfill is forbidden"
        )


async def collect(
    path: Path,
    day: date,
    fetch: BatchFetch,
    *,
    interval: float = 15.0,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    progress: ProgressHook | None = None,
    timer: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, object]:
    if not 0 <= interval <= 3600:
        raise ValueError("interval must be between 0 and 3600 seconds")
    validate_time(day, clock())
    conn = open_connection(path)
    started = timer()
    current: CollectionProgress | None = None
    provider_elapsed = 0.0
    max_chunk_elapsed = 0.0

    def emit() -> CollectionProgress:
        nonlocal current
        assert current is not None
        current = replace(
            current,
            elapsed_seconds=round(timer() - started, 3),
            provider_elapsed_seconds=round(provider_elapsed, 3),
            max_chunk_elapsed_seconds=round(max_chunk_elapsed, 3),
        )
        if progress is not None:
            progress(current)
        return current

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
            raise SourceDataError("Exact-date trading calendar is unavailable")
        if not calendar[0]:
            return {"status": "non_trading_day", "snapshot_date": day.isoformat()}
        symbols = universe(conn, day)
        rows: list[dict[str, Any]] = []
        current = CollectionProgress(
            day.isoformat(),
            "prepare",
            len(symbols),
            (len(symbols) + 49) // 50,
            interval_seconds=round(interval, 3),
        )
        emit()
        for offset in range(0, len(symbols), 50):
            if offset:
                await sleep(interval)
            chunk = symbols[offset : offset + 50]
            current = replace(
                current, phase="fetch", chunk_index=offset // 50 + 1, chunk_size=len(chunk)
            )
            emit()
            validate_time(day, clock())
            chunk_started = timer()
            try:
                payload = await fetch(chunk)
            finally:
                duration = timer() - chunk_started
                provider_elapsed += duration
                max_chunk_elapsed = max(max_chunk_elapsed, duration)
            fetched = clock()
            fetched_iso = fetched.astimezone(UTC).isoformat()
            current = replace(
                current,
                phase="normalize",
                response_bytes=current.response_bytes
                + len(json.dumps(payload, ensure_ascii=False).encode()),
                first_fetched_at_utc=current.first_fetched_at_utc or fetched_iso,
                last_fetched_at_utc=fetched_iso,
            )
            emit()
            validate_time(day, fetched)
            rows.extend(normalize_batch(payload, chunk, fetched))
            current = replace(
                current,
                phase="between_chunks",
                chunks_completed=current.chunks_completed + 1,
                chunk_index=None,
                chunk_size=None,
            )
            emit()
        # All chunks have been accounted for; a failure above writes no canonical rows.
        current = replace(current, phase="store")
        emit()
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
        current = replace(current, phase="complete")
        emit()
        return {
            **asdict(current),
            "status": "saved",
            "snapshot_date": day.isoformat(),
            "expected_universe": len(symbols),
            "rows": len(rows),
            "unresolved": sum(row["fetch_status"] == "unresolved" for row in rows),
            "normal_nulls": {
                name: sum(row["fetch_status"] == "ok" and row[name] is None for row in rows)
                for name in FIELDS
            },
        }
    except Exception as exc:
        if current is None:
            raise
        raise CollectionFailure(exc, emit()) from exc
    finally:
        conn.close()
