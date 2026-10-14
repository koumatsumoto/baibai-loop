"""Web/batchの指標・Reading・市場状態・判断履歴を公開する読取境界。"""

from __future__ import annotations

import sqlite3
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from baibai_engine.foundation.redaction import redact_credentials
from baibai_engine.market_context.assessment.models import ASSESSMENT_STALE_DAYS
from baibai_engine.market_context.assessment.query import (
    assessment_payload,
    latest_assessment_payload,
    list_assessment_payloads,
)
from baibai_engine.market_context.indicators.definitions import SeriesDefinition, load_definitions
from baibai_engine.market_context.master import MasterSnapshot
from baibai_engine.market_context.reading.query import (
    READING_RULES_PATH,
    reading_snapshot,
)
from baibai_engine.market_context.reading.reader import build_store_observation_reader
from baibai_engine.market_context.security import build_security_market_context
from baibai_engine.market_context.snapshot import (
    PreparedMarketContext,
    build_market_snapshot,
    prepare_market_context_inputs,
)

from .sqlite import (
    connect_read_only,
    is_unwritten_store,
    read_rows,
)

type IndicatorGranularity = Literal["daily", "weekly", "monthly", "yearly"]


def indicator_fetch_health(path: Path) -> list[dict[str, object]]:
    """The latest provider run per series: did the last acquisition attempt succeed?

    Staleness alone cannot see a provider that just went silent: a monthly series stays
    inside its staleness threshold for weeks after its source stops answering. The run
    record knows immediately, so the health panel reads both.
    """

    rows = read_rows(
        path,
        """
            SELECT series_id, status, finished_at, record_count, error_message FROM (
                SELECT series_id, status, finished_at, record_count, error_message,
                       row_number() OVER (
                           PARTITION BY series_id ORDER BY finished_at DESC, run_id DESC
                       ) AS rank
                FROM provider_runs
            )
            WHERE rank = 1
            ORDER BY series_id
            """,
    )
    registered = _registry_by_id()
    return [
        {
            "series_id": str(row[0]),
            "status": str(row[1]),
            "finished_at": str(row[2]),
            "record_count": int(row[3]),
            # Masked on read as well as on write: rows recorded before the write
            # side masked them are still in the store and still get published.
            "error_message": None if row[4] is None else redact_credentials(str(row[4])),
        }
        for row in rows
        if str(row[0]) in registered
    ]


def indicator_series_names() -> dict[str, str]:
    """Return canonical indicator series display names for read-only consumers."""
    return {item.series_id: item.name for item in load_definitions().series}


def registered_indicator(series_id: str) -> dict[str, str | None] | None:
    """Registry display fields for one series, or None when no registry defines it.

    The registry defines a series before any store carries an observation of it, so a
    consumer configured from the registry can meet a series its store has never seen and
    still name it correctly. An id the registry does not define is a configuration error
    the consumer decides how to treat.
    """

    definition = _registry_by_id().get(series_id)
    if definition is None:
        return None
    return {
        "name": definition.name,
        "unit": definition.unit,
        "tradingview_symbol": definition.tradingview_symbol,
    }


def indicator_series(
    path: Path,
    *,
    series_id: str,
    start: date | None = None,
    end: date | None = None,
    granularity: IndicatorGranularity = "daily",
    limit: int | None = 36,
) -> dict[str, object] | None:
    if granularity not in {"daily", "weekly", "monthly", "yearly"}:
        raise ValueError(f"unsupported indicator granularity: {granularity}")
    if start is not None and end is not None and end < start:
        raise ValueError("indicator end must be on or after start")
    if not path.is_file():
        return None
    registry = _registry_by_id()
    if series_id not in registry:
        return None
    connection = connect_read_only(path)
    try:
        series = connection.execute(
            "SELECT name, unit FROM series WHERE series_id = ?", (series_id,)
        ).fetchone()
        if series is None:
            return None
        reader = build_store_observation_reader(
            connection,
            series=tuple(registry.values()),
        )
        observations = reader(
            series_id,
            start or date.min,
            end or date.max,
        )
    except sqlite3.OperationalError as error:
        if not is_unwritten_store(error, path):
            raise
        return None
    finally:
        connection.close()
    points = [
        {
            "observed_at": observation.observed_at.isoformat(),
            "value": observation.value,
        }
        for observation in observations
    ]
    points = _aggregate_period_end(points, granularity=granularity)
    if limit is not None:
        if limit < 1:
            raise ValueError("indicator limit must be positive")
        points = points[-limit:]
    return {
        "series_id": series_id,
        "name": str(series[0]),
        "unit": str(series[1]),
        "tradingview_symbol": _tradingview_symbols().get(series_id),
        "points": points,
    }


@lru_cache(maxsize=1)
def _registry_by_id() -> dict[str, SeriesDefinition]:
    return load_definitions().by_id()


@lru_cache(maxsize=1)
def _tradingview_symbols() -> dict[str, str]:
    return {
        item.series_id: item.tradingview_symbol
        for item in load_definitions().series
        if item.tradingview_symbol is not None
    }


def _aggregate_period_end(
    points: list[dict[str, object]],
    *,
    granularity: IndicatorGranularity,
) -> list[dict[str, object]]:
    if granularity == "daily":
        return points
    period_end: dict[tuple[int, ...], dict[str, object]] = {}
    for point in points:
        observed_at = date.fromisoformat(str(point["observed_at"]))
        key: tuple[int, ...]
        match granularity:
            case "weekly":
                iso_year, iso_week, _ = observed_at.isocalendar()
                key = (iso_year, iso_week)
            case "monthly":
                key = (observed_at.year, observed_at.month)
            case "yearly":
                key = (observed_at.year,)
            case _:
                raise AssertionError(f"unreachable indicator granularity: {granularity}")
        period_end[key] = point
    return list(period_end.values())


__all__ = [
    "ASSESSMENT_STALE_DAYS",
    "READING_RULES_PATH",
    "IndicatorGranularity",
    "MasterSnapshot",
    "PreparedMarketContext",
    "assessment_payload",
    "build_market_snapshot",
    "build_security_market_context",
    "indicator_fetch_health",
    "indicator_series",
    "indicator_series_names",
    "latest_assessment_payload",
    "list_assessment_payloads",
    "prepare_market_context_inputs",
    "reading_snapshot",
    "registered_indicator",
]
