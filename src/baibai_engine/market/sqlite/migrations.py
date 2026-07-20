"""Forward-only schema migrations for the single physical store (`market.sqlite`).

The store holds 675MB of API-rate-limited price/fundamentals cache, so a schema
change upgrades an existing file in place instead of forcing a full re-fetch.
`BASELINE_VERSION` is the oldest `user_version` the migration path accepts: a
store at exactly the baseline (or any later version below `LATEST_VERSION`) is
migrated forward one step at a time; an older store is rejected fail-fast because
no forward path reconstructs it. `LATEST_VERSION` is the current schema version
and equals `SQLITE_SCHEMA_VERSION`; a fresh store is created directly at the
latest DDL and never replays migrations.

To evolve the schema: add a `Migration` with the next `version` (contiguous, no
gaps) and bump `SQLITE_SCHEMA_VERSION` in `schema.py` to match. Additive changes
that append a column at the DDL tail may use a plain `ALTER TABLE ... ADD COLUMN`
statement. A change that reorders or drops columns must instead rebuild the table
via `rebuild_table` in the migration's `transform` hook, because the strict shape
check compares column order against the DDL and an appended column would fail it.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass

# The oldest schema version the forward-migration path accepts. A store below
# this is rejected (delete-and-rebuild); a store between here and LATEST_VERSION
# is migrated up. This is the version at which version-managed migrations begin.
BASELINE_VERSION = 13

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class Migration:
    """One forward step from `version - 1` to `version`.

    `statements` are plain SQL run in order; `transform` is an optional procedural
    hook (e.g. one calling `rebuild_table`) run after them in the same transaction.
    """

    version: int
    statements: tuple[str, ...] = ()
    transform: Callable[[sqlite3.Connection], None] | None = None


# No migrations past the baseline yet: the store is created directly at the
# baseline DDL. Each schema change appends the next contiguous version here.
MIGRATIONS: tuple[Migration, ...] = ()

LATEST_VERSION = MIGRATIONS[-1].version if MIGRATIONS else BASELINE_VERSION


def rebuild_table(
    conn: sqlite3.Connection,
    *,
    table: str,
    create_statements: Sequence[str],
    columns: Sequence[str],
) -> None:
    """Rebuild `table` under new DDL, copying `columns` from the old rows.

    The forward-only template for a change that reorders or drops columns:
    rename the existing table aside, create the new table (and its indexes) from
    `create_statements`, copy the retained `columns` across, then drop the old
    table. `columns` must exist in both the old and new table; a dropped column is
    simply omitted from `columns`.

    Runs inside the caller's migration transaction and issues only single
    statements (never `executescript`, which would commit that transaction).
    """
    if not _IDENTIFIER.match(table):
        raise ValueError(f"invalid table identifier: {table!r}")
    if not columns:
        raise ValueError("rebuild_table requires at least one carried column")
    for column in columns:
        if not _IDENTIFIER.match(column):
            raise ValueError(f"invalid column identifier: {column!r}")
    temp_table = f"_migrate_{table}"
    column_list = ", ".join(columns)
    conn.execute(f"ALTER TABLE {table} RENAME TO {temp_table}")
    for statement in create_statements:
        conn.execute(statement)
    conn.execute(f"INSERT INTO {table}({column_list}) SELECT {column_list} FROM {temp_table}")
    conn.execute(f"DROP TABLE {temp_table}")


__all__ = [
    "BASELINE_VERSION",
    "LATEST_VERSION",
    "MIGRATIONS",
    "Migration",
    "rebuild_table",
]
