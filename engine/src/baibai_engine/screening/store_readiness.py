"""Whether the market store can be read at the current schema, and if not, why."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION, connect_current


def unreadable_store_reason(sqlite_path: Path) -> str | None:
    """Say why the store cannot be read at the current schema, or ``None`` if it can.

    Readers that answer a question degrade to "the cache cannot serve this" when the
    store is behind the code. A command that writes a measurement is not answering a
    question: it produces the record every later reading is taken from, and a degraded
    read turns that record into one holding nobody -- written, counted, and
    indistinguishable from a period the market genuinely had no data for. Those
    commands refuse instead.

    An old schema is the likely cause but not the only one, so the store's own version
    is read and reported rather than asserted: prescribing a multi-hour re-fetch for a
    corrupt or locked file would send the operator the wrong way.
    """
    if not sqlite_path.exists():
        return f"SQLite store not found: {sqlite_path}"
    conn = connect_current(sqlite_path)
    if conn is not None:
        conn.close()
        return None
    try:
        with closing(sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)) as probe:
            version = str(probe.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        version = f"unreadable ({type(exc).__name__})"
    detail = (
        "replace it with a current local build"
        if version != str(SQLITE_SCHEMA_VERSION)
        else "the schema version matches, so check the file for corruption or a lock"
    )
    return (
        f"SQLite store {sqlite_path} cannot be read at the current schema "
        f"(user_version {version}, expected {SQLITE_SCHEMA_VERSION}); {detail}"
    )
