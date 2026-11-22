"""Exact saved Article timestamps for Trend's SQL selection and recovery."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from functools import lru_cache

from pydantic import AwareDatetime, TypeAdapter, ValidationError

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_AWARE_CLOCK: TypeAdapter[datetime] = TypeAdapter(AwareDatetime)


def _clock_us(value: object) -> int | None:
    # Saved clocks are ISO text, not the numeric epochs Pydantic also accepts.
    if not isinstance(value, str) or value[4:5] != "-" or value[7:8] != "-":
        return None
    try:
        stamp = _AWARE_CLOCK.validate_python(value)
    except ValidationError:
        return None
    delta = stamp - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def ensure_trend_clock(connection: sqlite3.Connection) -> None:
    # SQLite can install once with a stepped cursor but cannot replace a live
    # function then. Probe connection-local state rather than cache connections.
    try:
        connection.execute("SELECT trend_clock_us(NULL)").close()
    except sqlite3.OperationalError as error:
        if str(error) != "no such function: trend_clock_us":
            raise
        # The same stamps are inspected in coverage, ranking and version queries.
        # Keep only short clock inputs, for the lifetime of this connection.
        cached = lru_cache(maxsize=2048)(_clock_us)

        def exact_clock(value: object) -> int | None:
            if isinstance(value, str) and len(value) <= 40:
                return cached(value)
            return _clock_us(value)

        connection.create_function("trend_clock_us", 1, exact_clock, deterministic=True)
