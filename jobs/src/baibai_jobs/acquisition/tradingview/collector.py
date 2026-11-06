"""Serial TradingView acquisition; retain each result before projecting its batch."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from datetime import (
    UTC,
    date,
    datetime,
)
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult

from baibai_engine.data_collection.providers.tradingview.accounting import (
    BatchAccountingError,
    account_batch,
)
from baibai_engine.data_collection.providers.tradingview.contract import (
    BATCH_TOOL,
    BATCH_TOOL_IDENTIFIERS,
)
from baibai_engine.data_collection.providers.tradingview.receipts import (
    read_tool_results,
    retain_tool_result,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview import (
    observations as observations_owner,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.contract import (
    COLUMNS,
    TABLE,
)
from baibai_engine.machine_analysis.observations.external_assessments.tradingview.decode import (
    decode_tool_result,
)
from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLiteSchemaError,
    open_connection,
    validate_current_schema,
)
from baibai_jobs.maintenance.tradingview_context import (
    calendar_context,
    stored_quality,
    universe,
    validate_time,
)
from baibai_jobs.observability.tradingview import (
    CollectionFailure,
    CollectionProgress,
    SourceDataError,
    TimeGuardError,
    failure_category,
    failure_line,
)
from baibai_jobs.schedule import tradingview_cycle_date, tradingview_observation_window

SOFT_BUDGET_SECONDS = 25 * 60
BatchFetch = Callable[[list[str], list[str]], Awaitable[CallToolResult]]


ProgressHook = Callable[[CollectionProgress], None]


def _complete_native_answer(result_json: str, symbols: list[str] | tuple[str, ...]) -> bool:
    try:
        data, _ = account_batch(decode_tool_result(result_json), symbols)
    except (observations_owner.FetchError, BatchAccountingError):
        return False
    return all(
        isinstance(row, dict) and set(observations_owner.COLUMNS) <= row.keys()
        for row in data.values()
    )


def _retained_coverage(
    conn: sqlite3.Connection, day: date, basis: date, expected: set[str], *, business: bool
) -> set[str]:
    """A complete native answer survives a failed numeric projection.

    This checks the actual request and response, not just receipt existence.
    Failed envelopes and incomplete requested fields remain eligible for a retry.
    Projection repair uses the explicit receipt rebuild, never a replacement GET.
    """
    identities = [
        row[0]
        for row in conn.execute(
            "SELECT receipt_id FROM tradingview_source_receipts WHERE snapshot_date=?",
            (day.isoformat(),),
        )
    ]
    covered: set[str] = set()
    for receipt in read_tool_results(conn, identities):
        requested = receipt.requested_symbols
        if (
            receipt.master_asof != basis
            or receipt.tool_name not in BATCH_TOOL_IDENTIFIERS
            or receipt.requested_columns != tuple(observations_owner.COLUMNS)
            or not 1 <= len(requested) <= 50
            or len(requested) != len(set(requested))
            or not set(requested) <= expected
        ):
            continue
        try:
            validate_time(day, receipt.received_at_utc, is_business_day=business)
        except observations_owner.FetchError:
            continue
        if _complete_native_answer(receipt.result_json, requested):
            covered.update(requested)
    return covered


def _snapshot_state(
    conn: sqlite3.Connection,
    day: date,
    *,
    master_asof: date | None = None,
    now: datetime | None = None,
    master_unavailable: bool = False,
) -> tuple[list[str] | None, dict[str, object]]:
    basis, business = calendar_context(conn, day)
    if master_asof is not None and master_asof != basis:
        raise SourceDataError("Observation master differs from calendar-required exact date")
    context: dict[str, object] = {
        "master_asof": basis.isoformat(),
        "is_business_day": business,
    }
    if now is not None:
        opening, _ = tradingview_observation_window(day, is_business_day=business)
        if now.tzinfo is None:
            raise TimeGuardError("Timezone-aware observation clock required")
        if now < opening and now.astimezone(opening.tzinfo).date() == day:
            return None, {
                **context,
                "snapshot_date": day.isoformat(),
                "status": "before_close" if business else "before_observation",
            }
        if tradingview_cycle_date(now) != day:
            raise TimeGuardError(
                "Snapshots require the current acquisition cycle; backfill is forbidden"
            )
        validate_time(day, now, is_business_day=business)
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
    if not has_master or master_unavailable:
        if not business or master_asof is not None:
            raise SourceDataError(
                "Observation requires a saved calendar-required exact-date master"
            )
        if stored and not master_unavailable:
            raise SourceDataError("Stored TradingView rows lack exact-date master")
        return None, {
            **context,
            **stored_quality(conn, day),
            "status": "needs_fetch",
            "snapshot_date": day.isoformat(),
            "needs_master": True,
            "rows": len(stored),
        }
    symbols = universe(conn, basis)
    expected = {symbol.removeprefix("TSE:") for symbol in symbols}
    if not stored <= expected:
        raise SourceDataError("Stored TradingView ticker is outside exact-date universe")
    native = _retained_coverage(conn, day, basis, set(symbols), business=business)
    missing_projection = {symbol for symbol in symbols if symbol.removeprefix("TSE:") not in stored}
    # L2 rows are not proof of a received request, including legacy partial copies.
    remaining = [symbol for symbol in symbols if symbol not in native]
    return remaining, {
        **context,
        **stored_quality(conn, day),
        "status": "needs_fetch"
        if remaining
        else "originals_saved"
        if missing_projection
        else "already_saved",
        "snapshot_date": day.isoformat(),
        "needs_master": False,
        "rows": len(stored),
        "expected_universe": len(symbols),
        "remaining": len(missing_projection),
        "remaining_requests": len(remaining),
        "projection_pending": len(missing_projection & native),
        "rows_without_originals": len(set(remaining) - missing_projection),
        "native_received": len(native),
        **(
            {"rows_added": 0, "native_added": 0, "requests_attempted": 0, "symbols_requested": 0}
            if not remaining
            else {}
        ),
    }


def preflight_snapshot(
    path: Path,
    day: date,
    *,
    master_asof: date | None = None,
    now: datetime | None = None,
    master_unavailable: bool = False,
) -> dict[str, object]:
    """Check exact-date completeness without creating or changing the market store."""
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            validate_current_schema(conn)
            _, result = _snapshot_state(
                conn, day, master_asof=master_asof, now=now, master_unavailable=master_unavailable
            )
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
        prior_tickers = {
            row[0]
            for row in conn.execute(
                "SELECT ticker FROM tradingview_forecast_snapshots WHERE snapshot_date=?",
                (day.isoformat(),),
            )
        }
        expected_universe = int(str(prior["expected_universe"]))
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
        request_columns = list(observations_owner.COLUMNS)
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
                if isinstance(exc, observations_owner.ProviderPayloadError):
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
                rows: list[dict[str, Any]] | None = observations_owner.normalize_batch(
                    payload, chunk, fetched
                )
            except observations_owner.ProviderPayloadError as exc:
                last_error = exc
                stop_reason = failure_category(exc)
                if not _complete_native_answer(result_json, chunk):
                    consecutive_bad += 1
                    if consecutive_bad >= 3:
                        break
                    continue
                # A complete original is L1 progress even when L2 cannot interpret it.
                rows = None
            except observations_owner.FetchError as exc:
                last_error = exc
                stop_reason = failure_category(exc)
                break
            # Acquire missing originals without silently replacing an existing L2 date.
            # Rebuilding that date from an explicit receipt selection is a separate action.
            if rows is not None:
                rows = [row for row in rows if row["ticker"] not in prior_tickers]
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
        if current.chunks_completed == 0:
            raise last_error or observations_owner.FetchError(
                "TradingView collection completed no requests"
            )
        current = replace(current, phase="complete")
        emit()
        _, final_state = _snapshot_state(conn, day, master_asof=master_asof)
        return {
            **asdict(current),
            "last_failure": failure_line(last_error) if last_error is not None else None,
            "status": "saved"
            if remaining == 0 and final_state["remaining_requests"] == 0
            else "partial",
            "snapshot_date": day.isoformat(),
            "expected_universe": expected_universe,
            "rows": total_rows,
            "rows_added": rows_added,
            "remaining": remaining,
            "remaining_requests": final_state["remaining_requests"],
            "projection_pending": final_state["projection_pending"],
            "rows_without_originals": final_state["rows_without_originals"],
            "native_received": final_state["native_received"],
            "native_added": int(str(final_state["native_received"]))
            - int(str(prior["native_received"])),
            "stop_reason": stop_reason,
            "unresolved": int(totals[1] or 0),
            "normal_nulls": stored_quality(conn, day)["normal_nulls"],
        }
    except Exception as exc:
        if current is None:
            raise
        raise CollectionFailure(exc, emit()) from exc
    finally:
        conn.close()
