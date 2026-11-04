"""Rebuild daily market prices from native L1 pages, without a prior L2 store."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest
from baibai_engine.data_collection.providers.jquants.day_selection import select_full_days
from baibai_engine.machine_analysis.observations.prices.jquants_normalization import (
    normalize_daily_bars,
)

if TYPE_CHECKING:
    import duckdb


@dataclass(frozen=True, slots=True)
class PriceDay:
    day: date
    request_id: str | None
    rows: tuple[tuple[Any, ...], ...]
    status: str
    rejected_count: int
    excluded_count: int
    incomplete_requests: int


def rebuild_price_days(
    requests: Iterable[RetainedRequest], *, days: Iterable[date]
) -> dict[date, PriceDay]:
    wanted = set(days)
    if not wanted:
        return {}
    selected, incomplete = select_full_days(requests, source="daily_bars", days=wanted)
    output: dict[date, PriceDay] = {}
    for day in sorted(wanted):
        chosen = selected.get(day)
        if chosen is None:
            output[day] = PriceDay(day, None, (), "unavailable", 0, 0, incomplete[day])
            continue
        identity, request = chosen
        normalized = normalize_daily_bars(request.records())
        if any(str(row[1]) != day.isoformat() for row in normalized.rows):
            raise ValueError("daily price payload contains a date outside the request")
        output[day] = PriceDay(
            day,
            identity,
            tuple(normalized.rows),
            normalized.status,
            normalized.rejected_count,
            normalized.excluded_count,
            incomplete[day],
        )
    return output


def materialize_price_days(
    connection: duckdb.DuckDBPyConnection, results: Iterable[PriceDay], *, l1_coverage_status: str
) -> None:
    if l1_coverage_status not in {"complete", "partial"}:
        raise ValueError("invalid L1 coverage status")
    days = tuple(results)
    if len({item.day for item in days}) != len(days):
        raise ValueError("price day appears more than once")
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(
            "CREATE TEMP TABLE daily_bars (ticker VARCHAR NOT NULL, traded_at VARCHAR NOT NULL, "
            "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, "
            "turnover_value DOUBLE, "
            "adjustment_factor DOUBLE, upper_limit VARCHAR, lower_limit VARCHAR, "
            "ex_right_type VARCHAR, "
            "market_cap_million_yen DOUBLE, PRIMARY KEY(ticker, traded_at))"
        )
        connection.execute(
            "CREATE TEMP TABLE price_receipt_status (day DATE PRIMARY KEY, request_id VARCHAR, "
            "projection_status VARCHAR NOT NULL, l1_coverage_status VARCHAR NOT NULL, "
            "rejected_count BIGINT NOT NULL, excluded_count BIGINT NOT NULL, "
            "incomplete_requests BIGINT NOT NULL)"
        )
        for result in days:
            if result.rows:
                connection.executemany(
                    "INSERT INTO daily_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", result.rows
                )
            connection.execute(
                "INSERT INTO price_receipt_status VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    result.day,
                    result.request_id,
                    result.status,
                    l1_coverage_status,
                    result.rejected_count,
                    result.excluded_count,
                    result.incomplete_requests,
                ],
            )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
