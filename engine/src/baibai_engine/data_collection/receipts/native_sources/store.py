"""Collection-owned economic inputs and source acquisition outcomes."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.primitives.redaction import redact_credentials
from baibai_engine.storage.paths import INDICATORS_DB_PATH
from baibai_engine.storage.sqlite.read import connect_read_only

SQLITE_SCHEMA_VERSION = 12
SCHEMA_PATH = Path(__file__).with_suffix(".sql")
DEFAULT_DB_PATH = INDICATORS_DB_PATH
_TABLES = {
    "economic_source_records": (
        ("source_id", "observed_at", "fetched_at", "record_count", "payload"),
        ("source_id", "observed_at", "fetched_at"),
    ),
    "economic_source_documents": (
        (
            "source_id",
            "request_key",
            "fetched_at",
            "source_url",
            "content_type",
            "payload",
            "metadata",
        ),
        ("source_id", "request_key", "fetched_at"),
    ),
    "economic_source_runs": (
        (
            "run_id",
            "source_id",
            "range_start",
            "range_end",
            "started_at",
            "finished_at",
            "status",
            "record_count",
            "error_message",
        ),
        ("run_id",),
    ),
}


RECEIPT_KEYS = {table: primary_key for table, (_, primary_key) in _TABLES.items()}


class EconomicSchemaError(RuntimeError):
    """The source store is missing or uses a different storage contract."""


def validate_current_schema(connection: sqlite3.Connection, *, schema: str = "main") -> None:
    if schema not in {"main", "source"}:
        raise ValueError(f"unsupported SQLite schema name: {schema!r}")
    trigger = connection.execute(
        f"SELECT name FROM {schema}.sqlite_master WHERE type = 'trigger' LIMIT 1"  # nosec B608
    ).fetchone()
    if trigger is not None:
        raise EconomicSchemaError("unexpected trigger in economic source store")
    version = connection.execute(f"PRAGMA {schema}.user_version").fetchone()[0]  # nosec B608
    if version != SQLITE_SCHEMA_VERSION:
        raise EconomicSchemaError(
            f"unsupported economic source schema: {version}; expected {SQLITE_SCHEMA_VERSION}"
        )
    for table, (columns, primary_key) in _TABLES.items():
        # Both identifiers come only from the allowlists above, not caller input.
        info = connection.execute(f"PRAGMA {schema}.table_info({table})").fetchall()  # nosec B608
        actual_columns = tuple(str(row[1]) for row in info)
        actual_key = tuple(str(row[1]) for row in sorted(info, key=lambda row: row[5]) if row[5])
        if actual_columns != columns or actual_key != primary_key:
            raise EconomicSchemaError(f"economic source table contract mismatch: {schema}.{table}")


def initialize_database(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            existing = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone()
            if existing is not None:
                raise EconomicSchemaError("a nonempty unversioned store is not a new source store")
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        validate_current_schema(connection)
        connection.commit()
    except BaseException:
        connection.close()
        raise
    return connection


def open_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    if not db_path.exists() and db_path.resolve() == DEFAULT_DB_PATH.resolve():
        raise FileNotFoundError("canonical economic source store requires explicit provisioning")
    return initialize_database(db_path)


def open_read_only_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    return connect_read_only(db_path)


def record_source_run(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    start: date,
    end: date,
    started_at: datetime,
    status: str,
    record_count: int,
    error_message: str | None = None,
) -> None:
    """Stage a source receipt outcome; the caller owns the transaction."""
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ValueError("source run time must include a timezone")
    connection.execute(
        "INSERT INTO economic_source_runs "
        "(run_id, source_id, range_start, range_end, started_at, finished_at, "
        "status, record_count, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"run-{uuid.uuid4().hex}",
            source_id,
            start.isoformat(),
            end.isoformat(),
            started_at.astimezone(UTC).isoformat(timespec="microseconds"),
            datetime.now(UTC).isoformat(timespec="microseconds"),
            status,
            record_count,
            None if error_message is None else redact_credentials(error_message),
        ),
    )


def row_count(connection: sqlite3.Connection, table: str) -> int:
    if table not in _TABLES:
        raise ValueError(f"unknown economic source table: {table}")
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])  # nosec B608
