"""Atomic append of immutable observations to the existing market SQLite."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from baibai_engine.machine_analysis.models.external_assessment.observations.models import Observation
from baibai_engine.machine_analysis.models.external_assessment.observations.rows import observation_row
from baibai_engine.storage.observations.schema import open_connection


class ObservationConflictError(ValueError):
    """A replay changed an observation at the same immutable received-time key."""


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
                table, names, values = observation_row(observation)
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
