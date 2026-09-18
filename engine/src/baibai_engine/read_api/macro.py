"""Query-only macro context and indicator views."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal, cast

from baibai_engine.foundation.redaction import redact_credentials
from baibai_engine.macro.context.models import (
    MACRO_CONTEXT_SCHEMA_VERSION,
    MACRO_CONTEXT_STALE_DAYS,
)
from baibai_engine.macro.indicators.definitions import SeriesDefinition, load_definitions
from baibai_engine.macro.reading.compute import compute_reading
from baibai_engine.macro.reading.models import snapshot_payload
from baibai_engine.macro.reading.reader import build_store_observation_reader
from baibai_engine.macro.reading.rules import (
    DEFAULT_RULES_PATH as MACRO_READING_RULES_PATH,
)
from baibai_engine.macro.reading.rules import load_reading_rules, rules_revision

from .sqlite import (
    connect_read_only,
    is_unwritten_store,
    read_application_rows,
    read_rows,
)

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
    store that has not been written yields None so a consumer can hide the panel.
    Rules are trusted configuration: read or validation failures propagate and stop
    materialization, because publishing a fresh generation with the reading silently
    absent is unsafe.
    """

    if not path.is_file():
        return None
    connection = connect_read_only(path)
    try:
        connection.execute("BEGIN")
        snapshot = _reading_on_connection(connection, asof=asof, rules_path=rules_path)
    except sqlite3.OperationalError as error:
        if not is_unwritten_store(error, path):
            raise
        return None
    finally:
        connection.close()
    return snapshot


def macro_series_fetch_health(path: Path) -> list[dict[str, object]]:
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
    try:
        from baibai_engine.appdb.read import connect_read_only as connect_application_read_only

        with_connection = connect_application_read_only(path)
        try:
            row = latest_macro_context_row(with_connection, as_of=as_of)
            return None if row is None else _context_payload(row["payload"])
        finally:
            with_connection.close()
    except sqlite3.OperationalError as error:
        if not is_unwritten_store(error, path):
            raise
        return None


def latest_macro_context_row(connection: sqlite3.Connection, *, as_of: date) -> sqlite3.Row | None:
    """Share the canonical as-of selector within a caller's read transaction."""
    return cast(
        sqlite3.Row | None,
        connection.execute(
            "SELECT * FROM macro_context WHERE as_of <= ? AND schema_version = ? "
            "ORDER BY published_at DESC, as_of DESC, context_id DESC LIMIT 1",
            (as_of.isoformat(), MACRO_CONTEXT_SCHEMA_VERSION),
        ).fetchone(),
    )


def list_macro_context_payloads(path: Path) -> list[dict[str, object]]:
    rows = read_application_rows(
        path,
        "SELECT payload FROM macro_context WHERE schema_version = ? "
        "ORDER BY published_at DESC, as_of DESC, context_id DESC",
        (MACRO_CONTEXT_SCHEMA_VERSION,),
    )
    return [_context_payload(row[0]) for row in rows]


def _context_payload(raw: object) -> dict[str, object]:
    payload = json.loads(str(raw))
    if not isinstance(payload, dict):
        raise ValueError("macro context payload must be an object")
    return payload


def macro_context_payload(
    path: Path,
    *,
    context_id: str,
    as_of: date,
) -> dict[str, object]:
    # An absent store and an unknown id send the operator to different places, so
    # this reader names which one it hit rather than reporting the id as wrong.
    if not path.is_file():
        raise ValueError(f"application database not found: {path}")
    rows = read_application_rows(
        path,
        "SELECT as_of, payload FROM macro_context WHERE context_id = ? AND schema_version = ?",
        (context_id, MACRO_CONTEXT_SCHEMA_VERSION),
    )
    if not rows:
        raise ValueError(f"unknown context_id: {context_id}")
    row = rows[0]
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


def stored_macro_rows(
    path: Path,
    *,
    kind: str,
    filters: dict[str, object],
    after: list[str | int | float] | None,
    limit: int,
    exact: bool = False,
) -> list[dict[str, object]]:
    """Read raw acquisition rows or the owner's effective observation selection."""
    from dataclasses import asdict

    from baibai_engine.foundation.sqlite_pages import select_page
    from baibai_engine.macro.indicators.db import (
        observations_in_range,
        open_read_only_connection,
        validate_current_schema,
    )
    from baibai_engine.macro.indicators.read_contracts import point_in_time_providers

    from .stored import required_read

    with required_read(path, open_read_only_connection) as connection:
        validate_current_schema(connection)
        if kind == "observations":
            series_id = str(filters["series_id"])
            if (
                not connection.execute(
                    "SELECT 1 FROM observations WHERE series_id = ? LIMIT 1", (series_id,)
                ).fetchone()
                and series_id not in load_definitions().by_id()
            ):
                raise FileNotFoundError("series unavailable")
            if not exact and filters.get("mode", "effective") == "effective":
                definition = load_definitions().by_id().get(series_id)
                if definition is None:
                    raise FileNotFoundError("effective series unavailable")
                values = observations_in_range(
                    connection,
                    series_id,
                    date.fromisoformat(str(filters["from"])),
                    date.fromisoformat(str(filters["to"])),
                    point_in_time=definition.provider in point_in_time_providers(),
                    vintage_on_or_before=date.fromisoformat(
                        str(filters.get("as_of") or filters["to"])
                    ),
                    after=None if after is None else (str(after[0]), str(after[1])),
                    limit=limit,
                )
                return [
                    {
                        key: value.isoformat() if isinstance(value, date) else value
                        for key, value in asdict(row).items()
                    }
                    for row in values
                ]
            equal: dict[str, object] = {
                key: filters[key]
                for key in ("series_id", "observed_at", "vintage_at")
                if key in filters
            }
            ranges = [
                ("observed_at", op, filters[key])
                for key, op in (("from", ">="), ("to", "<="))
                if key in filters
            ]
            return select_page(
                connection,
                table="observations",
                order=("observed_at", "vintage_at"),
                equal=equal,
                ranges=ranges,
                after=after,
                limit=limit,
            )
        if kind != "provider_runs":
            raise ValueError("unknown macro row kind")
        from baibai_engine.foundation.sqlite_pages import jst_day_ranges

        return select_page(
            connection,
            table="provider_runs",
            columns="*, julianday(finished_at) AS page_time",
            order=("julianday(finished_at)", "run_id"),
            equal={key: filters[key] for key in ("series_id", "run_id") if key in filters},
            ranges=jst_day_ranges("finished_at", filters),
            after=after,
            limit=limit,
        )


def _reading_on_connection(
    connection: sqlite3.Connection, *, asof: date, rules_path: Path
) -> dict[str, object]:
    definitions = load_definitions()
    return snapshot_payload(
        compute_reading(
            series=definitions.series,
            reader=build_store_observation_reader(connection, series=definitions.series),
            rules=load_reading_rules(rules_path),
            rules_revision=rules_revision(rules_path),
            asof=asof,
        )
    )


def stored_macro_reading(path: Path, *, asof: date) -> dict[str, object]:
    from baibai_engine.macro.indicators.db import open_read_only_connection, validate_current_schema

    from .stored import required_read

    with required_read(path, open_read_only_connection) as connection:
        validate_current_schema(connection)
        return _reading_on_connection(connection, asof=asof, rules_path=MACRO_READING_RULES_PATH)
