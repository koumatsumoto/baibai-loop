"""Serial, resumable TradingView collection with one commit per valid batch."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from datetime import time as daytime
from pathlib import Path
from zoneinfo import ZoneInfo

from mcp.types import CallToolResult

from baibai_engine.collection.tradingview.contract import BATCH_TOOL
from baibai_engine.collection.tradingview.receipts import read_tool_results, retain_tool_result
from baibai_engine.external_assessment.tradingview.contract import COLUMNS, TABLE
from baibai_engine.external_assessment.tradingview.decode import decode_tool_result
from baibai_engine.external_assessment.tradingview.diagnostics import (
    CollectionFailure as CollectionFailure,
)
from baibai_engine.external_assessment.tradingview.diagnostics import (
    CollectionProgress as CollectionProgress,
)
from baibai_engine.external_assessment.tradingview.diagnostics import (
    SourceDataError as SourceDataError,
)
from baibai_engine.external_assessment.tradingview.diagnostics import (
    TimeGuardError as TimeGuardError,
)
from baibai_engine.external_assessment.tradingview.diagnostics import failure_category, failure_line
from baibai_engine.external_assessment.tradingview.observations import (
    COLUMNS as REQUEST_COLUMNS,
)
from baibai_engine.external_assessment.tradingview.observations import (
    FIELDS,
    FetchError,
    ProviderPayloadError,
    normalize_batch,
)
from baibai_engine.market.calendar_context import observation_calendar_context
from baibai_engine.market.master_snapshot import masters_from_payloads
from baibai_engine.market.universe import ELIGIBLE_MARKETS
from baibai_engine.storage.observations.schema import (
    SQLiteSchemaError,
    open_connection,
    validate_current_schema,
)

JST = ZoneInfo("Asia/Tokyo")
SOFT_BUDGET_SECONDS = 25 * 60
BatchFetch = Callable[[list[str], list[str]], Awaitable[CallToolResult]]


ProgressHook = Callable[[CollectionProgress], None]


def universe(conn: sqlite3.Connection, day: date) -> list[str]:
    rows = conn.execute(
        "SELECT record_json FROM jquants_master_snapshots "
        "WHERE snapshot_date=? ORDER BY security_code",
        (day.isoformat(),),
    ).fetchall()
    if not rows:
        raise SourceDataError("Exact-date J-Quants master is unavailable")
    result = []
    for security in masters_from_payloads([row[0] for row in rows]):
        if not security.is_common_stock or security.market_segment.upper() not in ELIGIBLE_MARKETS:
            continue
        if not re.fullmatch(r"[0-9][0-9A-Z]{3}", security.ticker):
            raise SourceDataError("Invalid J-Quants ticker")
        result.append("TSE:" + security.ticker)
    if not result:
        raise SourceDataError("Empty TradingView universe")
    return result


def validate_time(day: date, now: datetime, *, is_business_day: bool = True) -> None:
    if now.tzinfo is None:
        raise ValueError("Timezone-aware observation clock required")
    local = now.astimezone(JST)
    opening = daytime(15, 30) if is_business_day else daytime(9)
    if local.date() != day or local.time() < opening:
        raise TimeGuardError(
            "Snapshots require today's permitted observation time; backfill is forbidden"
        )


def _stored_quality(conn: sqlite3.Connection, day: date) -> dict[str, object]:
    # Identifiers are fixed by the physical snapshot contract.
    null_counts = ",".join(f"SUM(fetch_status='ok' AND {name} IS NULL)" for name in FIELDS)
    row = conn.execute(
        f"SELECT SUM(fetch_status='unresolved'),MIN(fetched_at_utc),MAX(fetched_at_utc),"  # nosec B608
        f"{null_counts} FROM {TABLE} WHERE snapshot_date=?",
        (day.isoformat(),),
    ).fetchone()
    return {
        "unresolved": int(row[0] or 0),
        "first_fetched_at_utc": row[1],
        "last_fetched_at_utc": row[2],
        "normal_nulls": {
            name: int(count or 0) for name, count in zip(FIELDS, row[3:], strict=True)
        },
    }


def calendar_context(conn: sqlite3.Connection, day: date) -> tuple[date, bool]:
    """Keep the collector's source error contract around the shared saved-calendar rule."""
    try:
        return observation_calendar_context(conn, day)
    except ValueError as error:
        raise SourceDataError(str(error)) from error


def _snapshot_state(
    conn: sqlite3.Connection,
    day: date,
    *,
    master_asof: date | None = None,
    now: datetime | None = None,
) -> tuple[list[str] | None, dict[str, object]]:
    basis, business = calendar_context(conn, day)
    if master_asof is not None and master_asof != basis:
        raise SourceDataError("Observation master differs from calendar-required exact date")
    context: dict[str, object] = {
        "master_asof": basis.isoformat(),
        "is_business_day": business,
    }
    if now is not None:
        if now.tzinfo is None or now.astimezone(JST).date() != day:
            raise TimeGuardError("Snapshots require today's observation; backfill is forbidden")
        if now.astimezone(JST).time() < (daytime(15, 30) if business else daytime(9)):
            return None, {
                **context,
                "snapshot_date": day.isoformat(),
                "status": "before_close" if business else "before_observation",
            }
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
            (basis.isoformat(),),
        ).fetchone()
        is not None
    )
    if not has_master:
        if not business or master_asof is not None:
            raise SourceDataError(
                "Observation requires a saved calendar-required exact-date master"
            )
        if stored:
            raise SourceDataError("Stored TradingView rows lack exact-date master")
        return None, {
            **context,
            **_stored_quality(conn, day),
            "status": "needs_fetch",
            "snapshot_date": day.isoformat(),
            "needs_master": True,
            "rows": 0,
        }
    symbols = universe(conn, basis)
    expected = {symbol.removeprefix("TSE:") for symbol in symbols}
    if not stored <= expected:
        raise SourceDataError("Stored TradingView ticker is outside exact-date universe")
    remaining = [symbol for symbol in symbols if symbol.removeprefix("TSE:") not in stored]
    if not remaining:
        return remaining, {
            **context,
            **_stored_quality(conn, day),
            "status": "already_saved",
            "snapshot_date": day.isoformat(),
            "rows": len(stored),
            "expected_universe": len(symbols),
            "remaining": 0,
            "rows_added": 0,
            "requests_attempted": 0,
            "symbols_requested": 0,
        }
    return remaining, {
        **context,
        **_stored_quality(conn, day),
        "status": "needs_fetch",
        "snapshot_date": day.isoformat(),
        "needs_master": False,
        "rows": len(stored),
        "expected_universe": len(symbols),
        "remaining": len(remaining),
    }


def preflight_snapshot(
    path: Path, day: date, *, master_asof: date | None = None, now: datetime | None = None
) -> dict[str, object]:
    """Check exact-date completeness without creating or changing the market store."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            validate_current_schema(conn)
            _, result = _snapshot_state(conn, day, master_asof=master_asof, now=now)
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
    master_asof: date | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    progress: ProgressHook | None = None,
    timer: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, object]:
    if not 0 <= interval <= 3600:
        raise ValueError("interval must be between 0 and 3600 seconds")
    if not 1 <= batch_size <= 50:
        raise ValueError("batch_size must be between 1 and 50")
    if master_asof is not None and (batch_size != 50 or interval < 15):
        raise ValueError("Manual observations require batches of 50 and at least 15 seconds")
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
        symbols, prior = _snapshot_state(conn, day, master_asof=master_asof)
        business = bool(prior["is_business_day"])
        validate_time(day, clock(), is_business_day=business)
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
            master_asof=str(prior["master_asof"]),
        )
        emit()
        names = [column[0] for column in COLUMNS]
        request_columns = list(REQUEST_COLUMNS)
        basis = date.fromisoformat(str(prior["master_asof"]))
        sql = f"INSERT INTO {TABLE} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})"  # nosec B608
        for offset in range(0, len(symbols), batch_size):
            if offset:
                if timer() - started + interval >= SOFT_BUDGET_SECONDS:
                    stop_reason = "soft_budget"
                    break
                await sleep(interval)
            if timer() - started >= SOFT_BUDGET_SECONDS:
                stop_reason = "soft_budget"
                break
            try:
                validate_time(day, clock(), is_business_day=business)
            except TimeGuardError as exc:
                last_error = exc
                stop_reason = "time_guard"
                break
            chunk = symbols[offset : offset + batch_size]
            current = replace(
                current,
                phase="fetch",
                chunk_index=offset // batch_size + 1,
                chunk_size=len(chunk),
                requests_attempted=current.requests_attempted + 1,
                symbols_requested=current.symbols_requested + len(chunk),
            )
            emit()
            chunk_started = timer()
            try:
                result = await fetch(chunk, request_columns)
            except Exception as exc:
                category = failure_category(exc)
                if category in {"internal", "storage"}:
                    raise
                last_error = exc
                if isinstance(exc, ProviderPayloadError):
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
            receipt_id = retain_tool_result(
                conn,
                snapshot_date=day,
                master_asof=basis,
                tool_name=BATCH_TOOL,
                received_at_utc=fetched.astimezone(UTC),
                requested_symbols=chunk,
                requested_columns=request_columns,
                result=result,
            )
            result_json = read_tool_results(conn, (receipt_id,))[0].result_json
            current = replace(
                current,
                phase="normalize",
                response_bytes=current.response_bytes + len(result_json.encode()),
            )
            emit()
            try:
                validate_time(day, fetched, is_business_day=business)
            except TimeGuardError as exc:
                last_error = exc
                stop_reason = "time_guard"
                break
            try:
                payload = decode_tool_result(result_json)
                rows = normalize_batch(payload, chunk, fetched)
            except ProviderPayloadError as exc:
                consecutive_bad += 1
                last_error = exc
                stop_reason = failure_category(exc)
                if consecutive_bad >= 3:
                    break
                continue
            except FetchError as exc:
                last_error = exc
                stop_reason = failure_category(exc)
                break
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
            "last_failure": failure_line(last_error) if last_error is not None else None,
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
