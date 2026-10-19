"""Offline replacement of selected L2 observations from retained L1 attempts."""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.collection.equity_web.receipts import read_responses
from baibai_engine.external_assessment.normalization.decode import decode_attempt
from baibai_engine.external_assessment.observations.contract import (
    ANALYST_RATING_COLUMNS,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_COLUMNS,
    MODEL_VALUATION_TABLE,
    QUANTS_SCORE_COLUMNS,
    QUANTS_SCORE_TABLE,
)
from baibai_engine.external_assessment.observations.models import (
    AnalystRatingObservation,
    ModelValuationObservation,
    Observation,
    QuantsScoreObservation,
)
from baibai_engine.storage.observations.schema import validate_current_schema


def _selected_attempts(attempt_ids: Sequence[str]) -> tuple[str, ...]:
    if isinstance(attempt_ids, (str, bytes)) or not attempt_ids:
        raise ValueError("rebuild requires explicit attempt IDs")
    selected = tuple(attempt_ids)
    for attempt_id in selected:
        try:
            identity = uuid.UUID(attempt_id)
            if str(identity) != attempt_id or identity.int == 0:
                raise ValueError
        except (TypeError, ValueError, AttributeError):
            raise ValueError("attempt ID must be a canonical UUID") from None
    if len(set(selected)) != len(selected):
        raise ValueError("duplicate attempt ID")
    return selected


def _row(observation: Observation) -> tuple[str, tuple[str, ...], tuple[Any, ...]]:
    if isinstance(observation, ModelValuationObservation):
        table, columns = MODEL_VALUATION_TABLE, MODEL_VALUATION_COLUMNS
    elif isinstance(observation, AnalystRatingObservation):
        table, columns = ANALYST_RATING_TABLE, ANALYST_RATING_COLUMNS
    elif isinstance(observation, QuantsScoreObservation):
        table, columns = QUANTS_SCORE_TABLE, QUANTS_SCORE_COLUMNS
    else:
        raise TypeError("unsupported external assessment observation")
    names = tuple(column[0] for column in columns)
    payload = observation.model_dump()
    values = tuple(
        value.isoformat(timespec="microseconds")
        if isinstance(value, datetime)
        else value.isoformat()
        if isinstance(value, date)
        else value
        for name in names
        for value in (payload[name],)
    )
    return table, names, values


def rebuild_attempts(sqlite_path: Path, *, attempt_ids: Sequence[str]) -> int:
    """Atomically replace only selected existing-key L2 rows; never acquire pages."""
    selected = _selected_attempts(attempt_ids)
    uri = sqlite_path.resolve().as_uri() + "?mode=rw"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            validate_current_schema(conn)
            rows: list[tuple[str, tuple[str, ...], tuple[Any, ...]]] = []
            keys: set[tuple[str, str, str]] = set()
            for attempt_id in selected:
                receipt_ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT receipt_id FROM equity_web_source_responses "
                        "WHERE attempt_id=? ORDER BY receipt_id",
                        (attempt_id,),
                    )
                ]
                if not receipt_ids:
                    raise ValueError("selected attempt is not retained")
                table, names, values = _row(decode_attempt(read_responses(conn, receipt_ids)))
                key = (table, values[0], values[1])
                if key in keys:
                    raise ValueError("selected attempts produce a duplicate output key")
                keys.add(key)
                rows.append((table, names, values))
            for table, names, values in rows:
                conn.execute(
                    f"DELETE FROM {table} WHERE ticker=? AND fetched_at_utc=?",  # nosec B608
                    values[:2],
                )
                conn.execute(
                    f"INSERT INTO {table}({','.join(names)}) VALUES "  # nosec B608
                    f"({','.join('?' for _ in names)})",
                    values,
                )
            conn.commit()
            return len(rows)
        except BaseException:
            conn.rollback()
            raise
