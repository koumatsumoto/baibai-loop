"""保存済み観測からのMacro Readingと保存Contextの領域読取。取得・公開・UI合成へ依存しない。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import cast

from baibai_engine.foundation.sqlite_read import connect_read_only, is_unwritten_store, read_rows
from baibai_engine.macro.context.models import (
    MACRO_CONTEXT_SCHEMA_VERSION,
)
from baibai_engine.macro.context.models import (
    MACRO_CONTEXT_STALE_DAYS as MACRO_CONTEXT_STALE_DAYS,
)
from baibai_engine.market_context.indicators.definitions import load_definitions
from baibai_engine.market_context.reading.compute import compute_reading
from baibai_engine.market_context.reading.models import ReadingSnapshot, snapshot_payload
from baibai_engine.market_context.reading.reader import build_store_observation_reader
from baibai_engine.market_context.reading.rules import (
    DEFAULT_RULES_PATH as MACRO_READING_RULES_PATH,
)
from baibai_engine.market_context.reading.rules import load_reading_rules, rules_revision
from baibai_engine.storage.application.read import (
    connect_read_only as connect_application_read_only,
)


def _read_application_rows(
    path: Path, sql: str, parameters: tuple[object, ...]
) -> list[sqlite3.Row]:
    return read_rows(path, sql, parameters, connector=connect_application_read_only)


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
        snapshot = snapshot_payload(
            read_macro_snapshot(connection, asof=asof, rules_path=rules_path)
        )
    except sqlite3.OperationalError as error:
        if not is_unwritten_store(error, path):
            raise
        return None
    finally:
        connection.close()
    return snapshot


def latest_macro_context_payload(path: Path, *, as_of: date) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
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
    rows = _read_application_rows(
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
    rows = _read_application_rows(
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


def read_macro_snapshot(
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


__all__ = [
    "MACRO_CONTEXT_STALE_DAYS",
    "MACRO_READING_RULES_PATH",
    "latest_macro_context_payload",
    "latest_macro_context_row",
    "list_macro_context_payloads",
    "macro_context_payload",
    "macro_reading_snapshot",
    "read_macro_snapshot",
]
