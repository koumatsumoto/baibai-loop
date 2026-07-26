"""Query-only macro context and indicator views."""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from baibai_engine.macro.context.diagnostics import MACRO_CONTEXT_STALE_DAYS
from baibai_engine.macro.context.models import MACRO_CONTEXT_SCHEMA_VERSION
from baibai_engine.macro.indicators import db as indicators_db
from baibai_engine.macro.indicators.definitions import SeriesDefinition, load_definitions
from baibai_engine.macro.reading.compute import compute_reading
from baibai_engine.macro.reading.models import snapshot_payload
from baibai_engine.macro.reading.rules import (
    DEFAULT_RULES_PATH as MACRO_READING_RULES_PATH,
)
from baibai_engine.macro.reading.rules import (
    ReadingRulesError,
    load_reading_rules,
    rules_revision,
)

from .sqlite import connect_read_only

type MacroGranularity = Literal["daily", "weekly", "monthly", "yearly"]


def macro_reading_snapshot(
    path: Path,
    *,
    asof: date,
    rules_path: Path = MACRO_READING_RULES_PATH,
) -> dict[str, object] | None:
    """Compute the L2 reading from the indicator store, or None when it is unavailable.

    The reading is a pure function of the store, the rules revision and the as-of date,
    so a read-only consumer recomputes it instead of depending on a stored snapshot. A
    missing store or unreadable rules yields None so a consumer degrades to hiding the
    panel rather than failing the whole view.
    """

    if not path.is_file():
        return None
    try:
        rules = load_reading_rules(rules_path)
    except ReadingRulesError:
        return None
    connection = connect_read_only(path)
    try:
        snapshot = compute_reading(
            series=load_definitions().series,
            reader=lambda series_id, start, end: indicators_db.observations_in_range(
                connection, series_id, start, end
            ),
            rules=rules,
            rules_revision=rules_revision(rules_path),
            asof=asof,
        )
    finally:
        connection.close()
    return snapshot_payload(snapshot)


def macro_series_fetch_health(path: Path) -> list[dict[str, object]]:
    """The latest provider run per series: did the last acquisition attempt succeed?

    Staleness alone cannot see a provider that just went silent: a monthly series stays
    inside its staleness threshold for weeks after its source stops answering. The run
    record knows immediately, so the health panel reads both.
    """

    if not path.is_file():
        return []
    connection = connect_read_only(path)
    try:
        rows = connection.execute(
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
            """
        ).fetchall()
    finally:
        connection.close()
    registered = _registry_by_id()
    return [
        {
            "series_id": str(row[0]),
            "status": str(row[1]),
            "finished_at": str(row[2]),
            "record_count": int(row[3]),
            "error_message": None if row[4] is None else str(row[4]),
        }
        for row in rows
        if str(row[0]) in registered
    ]


def macro_series_names() -> dict[str, str]:
    """Return canonical macro series display names for read-only consumers."""
    return {item.series_id: item.name for item in load_definitions().series}


def macro_registered_series(series_id: str) -> dict[str, str | None] | None:
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


def latest_macro_context_payload(path: Path, *, as_of: date) -> dict[str, object] | None:
    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        row = connection.execute(
            """
            SELECT payload FROM macro_context
            WHERE as_of <= ? AND schema_version = ?
            ORDER BY published_at DESC, as_of DESC, context_id DESC
            LIMIT 1
            """,
            (as_of.isoformat(), MACRO_CONTEXT_SCHEMA_VERSION),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    payload = json.loads(str(row[0]))
    if not isinstance(payload, dict):
        raise ValueError("macro context payload must be an object")
    return payload


def list_macro_context_payloads(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    connection = connect_read_only(path)
    try:
        rows = connection.execute(
            "SELECT payload FROM macro_context WHERE schema_version = ? "
            "ORDER BY published_at DESC, as_of DESC, context_id DESC",
            (MACRO_CONTEXT_SCHEMA_VERSION,),
        ).fetchall()
    finally:
        connection.close()
    result: list[dict[str, object]] = []
    for row in rows:
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise ValueError("macro context payload must be an object")
        result.append(payload)
    return result


def macro_context_payload(
    path: Path,
    *,
    context_id: str,
    as_of: date,
) -> dict[str, object]:
    if not path.is_file():
        raise ValueError(f"application database not found: {path}")
    connection = connect_read_only(path)
    try:
        row = connection.execute(
            "SELECT as_of, payload FROM macro_context WHERE context_id = ? AND schema_version = ?",
            (context_id, MACRO_CONTEXT_SCHEMA_VERSION),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError(f"unknown context_id: {context_id}")
    context_as_of = date.fromisoformat(str(row[0]))
    if context_as_of > as_of:
        raise ValueError(
            f"future macro context is not eligible: {context_id} as_of={context_as_of}"
        )
    payload = json.loads(str(row[1]))
    if not isinstance(payload, dict):
        raise ValueError("macro context payload must be an object")
    return payload


def macro_indicator_series(
    path: Path,
    *,
    series_id: str,
    start: date | None = None,
    end: date | None = None,
    granularity: MacroGranularity = "daily",
    limit: int | None = 36,
) -> dict[str, object] | None:
    if granularity not in {"daily", "weekly", "monthly", "yearly"}:
        raise ValueError(f"unsupported macro granularity: {granularity}")
    if start is not None and end is not None and end < start:
        raise ValueError("macro indicator end must be on or after start")
    if not path.is_file():
        return None
    if series_id not in _registry_by_id():
        return None
    connection = connect_read_only(path)
    try:
        series = connection.execute(
            "SELECT name, unit, provider FROM series WHERE series_id = ?", (series_id,)
        ).fetchone()
        if series is None:
            return None
        start_text = start.isoformat() if start is not None else None
        end_text = end.isoformat() if end is not None else None
        rows = connection.execute(
            """
            SELECT observed_at, value, unit FROM (
                SELECT observed_at, value, unit,
                       row_number() OVER (
                           PARTITION BY observed_at ORDER BY vintage_at DESC
                       ) AS rank
                FROM observations
                WHERE series_id = ? AND fetch_status = 'ok'
                  AND (? IS NULL OR observed_at >= ?)
                  AND (? IS NULL OR observed_at <= ?)
                  AND (? != 'jquants_flows' OR ? IS NULL
                       OR substr(vintage_at, 1, 10) <= ?)
            )
            WHERE rank = 1
            ORDER BY observed_at ASC
            """,
            (
                series_id,
                start_text,
                start_text,
                end_text,
                end_text,
                str(series[2]),
                end_text,
                end_text,
            ),
        ).fetchall()
    finally:
        connection.close()
    points = [{"observed_at": str(row[0]), "value": float(row[1])} for row in rows]
    points = _aggregate_period_end(points, granularity=granularity)
    if limit is not None:
        if limit < 1:
            raise ValueError("macro indicator limit must be positive")
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
    granularity: MacroGranularity,
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
                raise AssertionError(f"unreachable macro granularity: {granularity}")
        period_end[key] = point
    return list(period_end.values())


__all__ = [
    # Re-exported so read-only consumers judge report freshness by the same policy the
    # engine's own consumers use, rather than keeping a second copy of the threshold.
    "MACRO_CONTEXT_STALE_DAYS",
    "MACRO_READING_RULES_PATH",
    "MacroGranularity",
    "latest_macro_context_payload",
    "list_macro_context_payloads",
    "macro_context_payload",
    "macro_indicator_series",
    "macro_reading_snapshot",
    "macro_registered_series",
    "macro_series_fetch_health",
    "macro_series_names",
]
