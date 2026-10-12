"""保存済み指標からReadingを計算する読取。取得・判断・公開・UI合成へ依存しない。"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from baibai_engine.foundation.sqlite_read import connect_read_only, is_unwritten_store
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.reading.compute import compute_reading
from baibai_engine.market_context.reading.models import ReadingSnapshot, snapshot_payload
from baibai_engine.market_context.reading.reader import build_store_observation_reader
from baibai_engine.market_context.reading.rules import (
    DEFAULT_RULES_PATH as READING_RULES_PATH,
)
from baibai_engine.market_context.reading.rules import load_reading_rules, rules_revision


def reading_snapshot(
    path: Path,
    *,
    asof: date,
    rules_path: Path = READING_RULES_PATH,
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
        snapshot = snapshot_payload(
            read_store_snapshot(connection, asof=asof, rules_path=rules_path)
        )
    except sqlite3.OperationalError as error:
        if not is_unwritten_store(error, path):
            raise
        return None
    finally:
        connection.close()
    return snapshot


def read_store_snapshot(
    connection: sqlite3.Connection, *, asof: date, rules_path: Path
) -> ReadingSnapshot:
    definitions = load_definitions()
    return compute_reading(
        series=definitions.series,
        reader=build_store_observation_reader(connection, series=definitions.series),
        rules=load_reading_rules(rules_path),
        rules_revision=rules_revision(rules_path),
        asof=asof,
    )


__all__ = ["READING_RULES_PATH", "read_store_snapshot", "reading_snapshot"]
