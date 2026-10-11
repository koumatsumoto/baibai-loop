"""Atomic append of immutable observations to the existing market SQLite."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.market.sqlite.schema import open_connection

from .contract import (
    ANALYST_RATING_COLUMNS,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_COLUMNS,
    MODEL_VALUATION_TABLE,
)
from .models import AnalystRatingObservation, ModelValuationObservation, Observation


class ObservationConflictError(ValueError):
    """A replay changed an observation at the same immutable received-time key."""


def _sql_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    return value


def save_observations(sqlite_path: Path, observations: Sequence[Observation]) -> int:
    """Return newly inserted rows; exact replays do nothing, any conflict rolls back."""
    if not observations:
        return 0
    conn = open_connection(sqlite_path)
    inserted = 0
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for observation in observations:
                if isinstance(observation, ModelValuationObservation):
                    table, columns = MODEL_VALUATION_TABLE, MODEL_VALUATION_COLUMNS
                elif isinstance(observation, AnalystRatingObservation):
                    table, columns = ANALYST_RATING_TABLE, ANALYST_RATING_COLUMNS
                else:
                    raise TypeError("unsupported retail dynamics observation")
                names = tuple(column[0] for column in columns)
                payload = observation.model_dump()
                values = tuple(_sql_value(payload[name]) for name in names)
                # Identifiers are leaf contract constants; observation values are bound.
                existing = conn.execute(
                    f"SELECT {','.join(names)} FROM {table} WHERE ticker=? AND fetched_at_utc=?",  # nosec B608
                    values[:2],
                ).fetchone()
                if existing is not None:
                    if existing != values:
                        raise ObservationConflictError(
                            f"conflicting immutable observation: {table}/{values[0]}/{values[1]}"
                        )
                    continue
                conn.execute(
                    f"INSERT INTO {table}({','.join(names)}) VALUES "  # nosec B608
                    f"({','.join('?' for _ in names)})",
                    values,
                )
                inserted += 1
    finally:
        conn.close()
    return inserted
