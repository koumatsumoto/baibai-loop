"""Serial, resumable TradingView collection with one commit per valid batch."""

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

from baibai_engine.market.sqlite import SQLiteSchemaError, open_connection, validate_current_schema
from baibai_engine.market.universe import (
    ELIGIBLE_MARKETS,
    NON_COMMON_STOCK_SECTORS,
    TSE_33_SECTORS,
    UniverseSourceDriftError,
)

from .contract import COLUMNS, TABLE
from .observations import COLUMNS as REQUEST_COLUMNS
from .observations import FIELDS, AllMissingError, FetchError, ProviderPayloadError, normalize_batch

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


def _snapshot_state(
    conn: sqlite3.Connection, day: date
) -> tuple[list[str] | None, dict[str, object]]:
    calendar = conn.execute(
        "SELECT is_business_day FROM jquants_market_calendar WHERE day=?", (day.isoformat(),)
    ).fetchone()
    if calendar is None:
        raise SourceDataError("Exact-date trading calendar is unavailable")
    if not calendar[0]:
        return None, {"status": "non_trading_day", "snapshot_date": day.isoformat()}
    stored = {
        row[0]
        for row in conn.execute(
            "SELECT ticker FROM tradingview_forecast_snapshots WHERE snapshot_date=?",
            (day.isoformat(),),
        )
    }
    has_master = (
        conn.execute(
            "SELECT 1 FROM jquants_master_snapshots WHERE snapshot_date=? LIMIT 1",
            (day.isoformat(),),
        ).fetchone()
        is not None
    )
    if not has_master:
        if stored:
            raise SourceDataError("Stored TradingView rows lack exact-date master")
        return None, {
            "status": "needs_fetch",
            "snapshot_date": day.isoformat(),
            "needs_master": True,
            "rows": 0,
        }
    symbols = universe(conn, day)
    expected = {symbol.removeprefix("TSE:") for symbol in symbols}
    if not stored <= expected:
        raise SourceDataError("Stored TradingView ticker is outside exact-date universe")
    remaining = [symbol for symbol in symbols if symbol.removeprefix("TSE:") not in stored]
    if not remaining:
        return remaining, {
            "status": "already_saved",
            "snapshot_date": day.isoformat(),
            "rows": len(stored),
        }
    return remaining, {
        "status": "needs_fetch",
        "snapshot_date": day.isoformat(),
        "needs_master": False,
        "rows": len(stored),
        "expected_universe": len(symbols),
        "remaining": len(remaining),
    }


def preflight_snapshot(path: Path, day: date) -> dict[str, object]:
    """Check exact-date completeness without creating or changing the market store."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            validate_current_schema(conn)
            _, result = _snapshot_state(conn, day)
            return result
        finally:
            conn.close()
    except (sqlite3.Error, SQLiteSchemaError) as exc:
        raise SourceDataError("Market SQLite is unavailable") from exc


async def collect(
    path: Path,
    day: date,
    fetch: BatchFetch,
    *,
    interval: float = 15.0,
    batch_size: int = 50,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    progress: ProgressHook | None = None,
    timer: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, object]:
    if not 0 <= interval <= 3600:
        raise ValueError("interval must be between 0 and 3600 seconds")
    if not 1 <= batch_size <= 50:
        raise ValueError("batch_size must be between 1 and 50")
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
        symbols, prior = _snapshot_state(conn, day)
        if prior["status"] != "needs_fetch":
            return prior
        if symbols is None:
            raise SourceDataError("Exact-date J-Quants master is unavailable")
        stored_rows = prior["rows"]
        assert isinstance(stored_rows, int)
        expected_universe = len(symbols) + stored_rows
        rows_added = 0
        consecutive_bad = 0
        stop_reason: str | None = None
        last_error: Exception | None = None
        current = CollectionProgress(
            day.isoformat(),
            "prepare",
            expected_universe,
            (len(symbols) + batch_size - 1) // batch_size,
            interval_seconds=round(interval, 3),
        )
        emit()
        names = [column[0] for column in COLUMNS]
        sql = f"INSERT INTO {TABLE} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})"  # nosec B608
        for offset in range(0, len(symbols), batch_size):
            if offset:
                await sleep(interval)
            if timer() - started >= 25 * 60:
                stop_reason = "soft_budget"
                break
            try:
                validate_time(day, clock())
            except TimeGuardError:
                stop_reason = "time_guard"
                break
            chunk = symbols[offset : offset + batch_size]
            current = replace(
                current, phase="fetch", chunk_index=offset // batch_size + 1, chunk_size=len(chunk)
            )
            emit()
            chunk_started = timer()
            try:
                payload = await fetch(chunk)
            except Exception as exc:
                from .cli import failure_category

                category = failure_category(exc)
                if category in {"internal", "storage"}:
                    raise
                last_error = exc
                if isinstance(exc, (AllMissingError, ProviderPayloadError)):
                    consecutive_bad += 1
                    stop_reason = category
                    if consecutive_bad >= 3:
                        stop_reason = category
                        break
                    continue
                stop_reason = category
                break
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
            )
            emit()
            try:
                validate_time(day, fetched)
            except TimeGuardError:
                stop_reason = "time_guard"
                break
            try:
                rows = normalize_batch(payload, chunk, fetched)
            except (AllMissingError, ProviderPayloadError) as exc:
                from .cli import failure_category

                consecutive_bad += 1
                last_error = exc
                stop_reason = failure_category(exc)
                if consecutive_bad >= 3:
                    break
                continue
            current = replace(current, phase="store")
            emit()
            conn.execute("BEGIN IMMEDIATE")
            existing = {
                row[0]
                for row in conn.execute(
                    "SELECT ticker FROM tradingview_forecast_snapshots WHERE snapshot_date=?",
                    (day.isoformat(),),
                )
            }
            if any(row["ticker"] in existing for row in rows):
                raise SourceDataError("TradingView row changed during collection")
            conn.executemany(
                sql,
                [
                    tuple(
                        day.isoformat() if name == "snapshot_date" else row.get(name)
                        for name in names
                    )
                    for row in rows
                ],
            )
            conn.commit()
            rows_added += len(rows)
            consecutive_bad = 0
            current = replace(
                current,
                phase="between_chunks",
                chunks_completed=current.chunks_completed + 1,
                chunk_index=None,
                chunk_size=None,
                first_fetched_at_utc=current.first_fetched_at_utc or fetched_iso,
                last_fetched_at_utc=fetched_iso,
            )
            emit()
        totals = conn.execute(
            "SELECT COUNT(*), SUM(fetch_status='unresolved') FROM tradingview_forecast_snapshots "
            "WHERE snapshot_date=?",
            (day.isoformat(),),
        ).fetchone()
        total_rows = int(totals[0])
        remaining = expected_universe - total_rows
        if rows_added == 0:
            raise last_error or FetchError("TradingView collection added no rows")
        current = replace(current, phase="complete")
        emit()
        return {
            **asdict(current),
            "status": "saved" if remaining == 0 else "partial",
            "snapshot_date": day.isoformat(),
            "expected_universe": expected_universe,
            "rows": total_rows,
            "rows_added": rows_added,
            "remaining": remaining,
            "stop_reason": stop_reason,
            "unresolved": int(totals[1] or 0),
            "normal_nulls": {
                # Table and column names come only from the fixed schema contract.
                name: conn.execute(
                    f"SELECT COUNT(*) FROM {TABLE} WHERE snapshot_date=? AND fetch_status='ok' "  # nosec B608
                    f"AND {name} IS NULL",
                    (day.isoformat(),),
                ).fetchone()[0]
                for name in FIELDS
            },
        }
    except Exception as exc:
        if current is None:
            raise
        raise CollectionFailure(exc, emit()) from exc
    finally:
        conn.close()
