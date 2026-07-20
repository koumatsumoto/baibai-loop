"""Query-only macro context and indicator views."""

from __future__ import annotations

import json
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from baibai_engine.macro.indicators.definitions import load_definitions

from .sqlite import connect_read_only

type MacroGranularity = Literal["daily", "weekly", "monthly", "yearly"]


def macro_series_names() -> dict[str, str]:
    """Return canonical macro series display names for read-only consumers."""
    return {item.series_id: item.name for item in load_definitions().series}


def latest_macro_context_payload(path: Path, *, as_of: date) -> dict[str, object] | None:
    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        row = connection.execute(
            """
            SELECT payload FROM macro_context
            WHERE as_of <= ?
            ORDER BY published_at DESC, as_of DESC, context_id DESC
            LIMIT 1
            """,
            (as_of.isoformat(),),
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
            "SELECT payload FROM macro_context "
            "ORDER BY published_at DESC, as_of DESC, context_id DESC"
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
            "SELECT as_of, payload FROM macro_context WHERE context_id = ?", (context_id,)
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
    "MacroGranularity",
    "latest_macro_context_payload",
    "list_macro_context_payloads",
    "macro_context_payload",
    "macro_indicator_series",
    "macro_series_names",
]
