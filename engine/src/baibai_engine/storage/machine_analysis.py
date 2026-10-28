"""機械分析の実行用DB接続とschema。モデルの計算・候補選択をしない。"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from baibai_engine.storage.paths import RUNS_DB_PATH

DEFAULT_RUN_STORE_PATH = RUNS_DB_PATH

RUN_STORE_SCHEMA_VERSION = 7

SCHEMA_SQL = """
CREATE TABLE screening_run (
    run_revision_id TEXT PRIMARY KEY,
    public_run_id TEXT NOT NULL,
    run_date TEXT NOT NULL,
    asof_date TEXT NOT NULL,
    run_at TEXT NOT NULL,
    universe_size INTEGER NOT NULL CHECK (universe_size >= 0),
    rules_ref TEXT,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (public_run_id, run_at)
) STRICT;
CREATE INDEX screening_run_asof_idx
ON screening_run (asof_date, run_at, run_revision_id);
CREATE TABLE stock_analysis (
    run_revision_id TEXT NOT NULL REFERENCES screening_run(run_revision_id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    ticker TEXT NOT NULL,
    sector_33 TEXT,
    per_forward REAL,
    per_trailing REAL,
    pbr REAL,
    dividend_yield REAL,
    er_annual REAL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_revision_id, ticker),
    UNIQUE (run_revision_id, ordinal)
) STRICT, WITHOUT ROWID;
CREATE INDEX stock_analysis_ticker_idx
ON stock_analysis (ticker, run_revision_id);
"""


GENERATION_SCHEMA_SQL = """
CREATE TABLE model_generation (
    generation_id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    evaluation_time TEXT NOT NULL,
    header TEXT NOT NULL
) STRICT;
CREATE TABLE generated_model (
    generation_id TEXT NOT NULL REFERENCES model_generation(generation_id),
    model_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    calculation_revision TEXT NOT NULL,
    payload_gzip BLOB NOT NULL,
    PRIMARY KEY (generation_id, model_id, subject)
) STRICT, WITHOUT ROWID;
CREATE TABLE screening_selection (
    selection_id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL REFERENCES model_generation(generation_id),
    published_at TEXT NOT NULL,
    payload_gzip BLOB NOT NULL
) STRICT;
CREATE INDEX screening_selection_generation_idx ON screening_selection (generation_id);
"""
SCHEMA_SQL += GENERATION_SCHEMA_SQL


def run_store_path(path: Path | None = None) -> Path:
    if path is not None:
        return path.expanduser()
    configured = os.environ.get("BAIBAI_RUNS_DB")
    return Path(configured).expanduser() if configured else DEFAULT_RUN_STORE_PATH


def connect_rw(path: Path | None = None) -> sqlite3.Connection:
    resolved = run_store_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(resolved, isolation_level=None)
    connection.row_factory = sqlite3.Row
    register_run_clock(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def run_clock(value: str) -> str:
    """Compare saved aware clocks as fixed-width UTC text at full microsecond precision."""
    return datetime.fromisoformat(value).astimezone(UTC).isoformat(timespec="microseconds")


def register_run_clock(connection: sqlite3.Connection) -> None:
    connection.create_function("run_clock", 1, run_clock, deterministic=True)


def initialize_run_store(path: Path | None = None) -> int:
    with closing(connect_rw(path)) as connection:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current == RUN_STORE_SCHEMA_VERSION:
            return current
        tables = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if current != 0 or tables is not None:
            raise RuntimeError(
                "machine-analysis store uses an obsolete schema; explicit migration is required "
                f"(found user_version={current}, expected={RUN_STORE_SCHEMA_VERSION})"
            )
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL)
            connection.execute(f"PRAGMA user_version = {RUN_STORE_SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return RUN_STORE_SCHEMA_VERSION


def connect_read_only(path: Path | None = None) -> sqlite3.Connection:
    """Open the run store read-only and require its current schema."""

    resolved = run_store_path(path).resolve()
    connection = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    register_run_clock(connection)
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        has_tables = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if version == 0 and has_tables is None:
            # An untouched file is the same no-store state as an absent path. The
            # first domain query raises OperationalError, which the caller reports as
            # no publication; it is not an obsolete store accepted as current.
            return connection
        if version != RUN_STORE_SCHEMA_VERSION:
            raise RuntimeError(
                "screening run store schema is not current "
                f"(found {version}, expected {RUN_STORE_SCHEMA_VERSION}); use explicit migration"
            )
        return connection
    except BaseException:
        connection.close()
        raise
