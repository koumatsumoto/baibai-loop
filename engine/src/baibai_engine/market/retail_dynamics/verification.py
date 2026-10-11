"""Read-only verification of staged receipts and complete retail history."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path

from baibai_engine.market.sqlite.read import connect_read_only

from .contract import (
    ANALYST_RATING_COLUMNS,
    ANALYST_RATING_TABLE,
    MODEL_VALUATION_COLUMNS,
    MODEL_VALUATION_TABLE,
)
from .models import ModelValuationObservation, Observation
from .reader import read_observations


def verify_observations(database: Path, observations: Sequence[Observation]) -> None:
    for item in observations:
        history = read_observations(database, item.ticker, item.fetched_at_utc)
        actual = (
            history.model_valuation.latest
            if isinstance(item, ModelValuationObservation)
            else history.analyst_rating.latest
        )
        if actual != item:
            raise ValueError("retail receipt readback mismatch")


def history_digests(database: Path) -> dict[str, str]:
    result = {}
    with closing(connect_read_only(database)) as connection:
        connection.execute("BEGIN")
        for table, columns in (
            (MODEL_VALUATION_TABLE, MODEL_VALUATION_COLUMNS),
            (ANALYST_RATING_TABLE, ANALYST_RATING_COLUMNS),
        ):
            digest = hashlib.sha256()
            names = ",".join(column[0] for column in columns)
            for row in connection.execute(
                f"SELECT {names} FROM {table} ORDER BY ticker,fetched_at_utc"  # nosec B608
            ):
                digest.update(
                    json.dumps(tuple(row), allow_nan=False, separators=(",", ":")).encode()
                )
                digest.update(b"\n")
            result[table] = digest.hexdigest()
    return result
