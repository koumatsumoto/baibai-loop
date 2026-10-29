"""Explicit, additive 48 -> 49 working-copy conversion for article rollout.

Never invoked by open_connection/daily. Jobs owns writer drain, fresh bundle/ETag,
publication and conditional machine cutover. This only copies an exact v48 store,
adds empty owner tables and preserves every existing table/value (including origin
and operator-owned data). It is also usable on a copied v48 input/stage; the
immutable source file must not be replaced. Normal merge remains current-only.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from baibai_engine.data_collection.receipts.articles.storage_contracts import ARTICLE_RECEIPTS_DDL
from baibai_engine.machine_analysis.observations.articles.storage_contracts import (
    ARTICLE_OBSERVATIONS_DDL,
)
from baibai_engine.storage.sqlite.source_cache.schema import (
    _SCHEMA_SQL,
    _expected_schema_shape,
    _index_info,
    _table_info,
    validate_current_schema,
)


def upgrade_article_working_copy(source: Path, destination: Path) -> None:
    """Create a new v49 copy; fail if source is not v48 or destination exists."""
    if source.resolve() == destination.resolve():
        raise ValueError("article upgrade requires a separate working copy")
    old = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
    try:
        old.execute("BEGIN")
        if old.execute("PRAGMA user_version").fetchone()[0] != 48:
            raise ValueError("article upgrade accepts only schema 48")
        old_ddl = _SCHEMA_SQL.replace(ARTICLE_RECEIPTS_DDL, "").replace(
            ARTICLE_OBSERVATIONS_DDL, ""
        )
        tables, indexes = _expected_schema_shape(old_ddl)
        for table, shape in tables.items():
            if _table_info(old, table) != shape:
                raise ValueError(f"schema 48 table differs: {table}")
        for name, shape in indexes.items():
            if _index_info(old, shape[0], name) != shape:
                raise ValueError(f"schema 48 index differs: {name}")
        names = {row[0] for row in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if any(name.startswith("article_") for name in names):
            raise ValueError("schema 48 source already has article tables")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL ensures a concurrent invocation cannot replace another work copy.
        with destination.open("xb"):
            pass
        target = sqlite3.connect(destination)
        try:
            old.backup(target)
            target.executescript(
                "BEGIN IMMEDIATE;"
                + ARTICLE_RECEIPTS_DDL
                + ARTICLE_OBSERVATIONS_DDL
                + "PRAGMA user_version=49;COMMIT;"
            )
            validate_current_schema(target)
        finally:
            target.close()
    finally:
        old.close()
