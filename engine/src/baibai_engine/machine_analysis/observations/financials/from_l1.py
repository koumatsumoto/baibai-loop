"""Rebuild one financial disclosure day from retained native L1 requests."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import duckdb

from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest
from baibai_engine.data_collection.providers.jquants.day_selection import select_full_days
from baibai_engine.machine_analysis.observations.financials.jquants_normalization import (
    normalize_financial_summaries,
)


@dataclass(frozen=True, slots=True)
class FinancialDay:
    """A derived day and its exact L1 source; completeness of L1 remains separate."""

    day: date
    request_id: str | None
    rows: tuple[tuple[Any, ...], ...]
    status: str
    rejected_count: int
    excluded_count: int
    incomplete_requests: int


def rebuild_financial_days(
    requests: Iterable[RetainedRequest], *, days: Iterable[date]
) -> dict[date, FinancialDay]:
    """Build a finite set of dates in one pass without reading previous L2.

    A result does not prove that L1 covers the requested date. Unknown-scope
    incomplete requests are reported for every date rather than silently lost.
    """
    wanted = set(days)
    if not wanted:
        return {}
    selected, incomplete = select_full_days(requests, source="financial_summaries", days=wanted)
    output: dict[date, FinancialDay] = {}
    for day in sorted(wanted):
        chosen = selected.get(day)
        if chosen is None:
            output[day] = FinancialDay(day, None, (), "unavailable", 0, 0, incomplete[day])
            continue
        identity, request = chosen
        normalized = normalize_financial_summaries(request.records())
        if any(str(row[1]) != day.isoformat() for row in normalized.rows):
            raise ValueError("financial payload contains a disclosure date outside the request")
        output[day] = FinancialDay(
            day=day,
            request_id=identity,
            rows=tuple(normalized.rows),
            status=normalized.status,
            rejected_count=normalized.rejected_count,
            excluded_count=normalized.excluded_count,
            incomplete_requests=incomplete[day],
        )
    return output


def rebuild_financial_day(requests: Iterable[RetainedRequest], *, day: date) -> FinancialDay:
    """Build one day from the same L1-only multi-day path."""
    return rebuild_financial_days(requests, days=(day,))[day]


def materialize_financial_days(
    connection: duckdb.DuckDBPyConnection,
    results: Iterable[FinancialDay],
    *,
    l1_coverage_status: str,
) -> None:
    """Create isolated DuckDB observations and explicit per-day receipt states.

    No L1 coverage or publication eligibility is inferred from a successful
    receipt. Partial rows remain available alongside their status.
    """
    if l1_coverage_status not in {"complete", "partial"}:
        raise ValueError("invalid L1 coverage status")
    days = tuple(results)
    if len({item.day for item in days}) != len(days):
        raise ValueError("financial day appears more than once")
    connection.execute("BEGIN TRANSACTION")
    try:
        connection.execute(
            "CREATE TEMP TABLE financial_summaries ("
            "ticker VARCHAR NOT NULL, disclosed_at VARCHAR NOT NULL, "
            "disclosure_number VARCHAR NOT NULL, document_payload VARCHAR NOT NULL, "
            "PRIMARY KEY(ticker, disclosed_at, disclosure_number))"
        )
        connection.execute(
            "CREATE TEMP TABLE financial_receipt_status ("
            "day DATE PRIMARY KEY, request_id VARCHAR, projection_status VARCHAR NOT NULL, "
            "l1_coverage_status VARCHAR NOT NULL, "
            "rejected_count BIGINT NOT NULL, excluded_count BIGINT NOT NULL, "
            "incomplete_requests BIGINT NOT NULL)"
        )
        for result in days:
            if result.rows:
                connection.executemany(
                    "INSERT INTO financial_summaries VALUES (?, ?, ?, ?)", result.rows
                )
            connection.execute(
                "INSERT INTO financial_receipt_status VALUES (?, ?, ?, ?, ?, ?, ?)",
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
