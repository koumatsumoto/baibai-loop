"""JPX original receipt stores, independent of derived cache schema versions."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from baibai_engine.data_collection.providers.jpx.contract import JPX_SOURCE_BUNDLES_DDL


@lru_cache(maxsize=1)
def _shapes() -> dict[str, tuple[tuple[object, ...], ...]]:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.executescript(JPX_SOURCE_BUNDLES_DDL)
        return {
            table: tuple(tuple(row) for row in conn.execute(f"PRAGMA table_xinfo({table})"))
            for table in ("jpx_source_bundles", "jpx_source_responses")
        }


def validate_receipts(conn: sqlite3.Connection) -> None:
    for table, expected in _shapes().items():
        actual = tuple(tuple(row) for row in conn.execute(f"PRAGMA table_xinfo({table})"))
        if actual != expected:
            raise ValueError("JPX receipt schema differs from its source contract")


def open_receipts(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(JPX_SOURCE_BUNDLES_DDL)
        validate_receipts(conn)
        return conn
    except BaseException:
        conn.close()
        raise
