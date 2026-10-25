"""The part of a store merge that does not depend on which store is being merged.

Two machine stores have the same publishing problem: the cloud writes recent facts on
a schedule while an operator deepens history locally, so each holds rows the other has
never seen and publishing means merging the cloud copy in first. The proof that nothing
is lost is the same in both cases — every shared key must carry the same payload, and no
source row may remain unmatched once the merge has run — so it lives here and each store
supplies only its own contracts.

The merge itself is an ``INSERT OR IGNORE`` per table, keyed by whatever the store says
identifies a row. A row only the source has is carried over verbatim.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

# SQLite cannot bind a table, column or schema name, so every statement below interpolates
# the ones it needs. Two guards keep that interpolation from carrying anything but a plain
# identifier: names the caller supplies go through `internal_name`, and names read out of a
# store's own schema go through `store_column`.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SCHEMAS = frozenset({"main", "source"})


class MergeError(RuntimeError):
    """The stores cannot be merged, so the target is left untouched."""


@dataclass(frozen=True, slots=True)
class RowFilter:
    """Which source rows a store considers eligible to carry, as a SQL predicate.

    Two spellings rather than one built on demand: the statements below reference the
    source table unaliased in some places and as ``s`` in others, and a predicate
    assembled from a caller-supplied prefix would be string-built SQL. Written out, both
    are constants this module never composes.
    """

    unaliased: str = "1"
    aliased: str = "1"


EVERY_ROW = RowFilter()


def internal_name(name: str) -> str:
    """Check a name the caller supplies itself; a rejection here is a bug, not bad data."""

    if _IDENTIFIER.match(name) is None:
        raise ValueError(f"invalid SQL identifier: {name!r}")
    return name


def schema_name(schema: str) -> str:
    """Only the two schemas a merge attaches may name a statement's tables."""

    if schema not in _SCHEMAS:
        raise ValueError(f"unsupported SQLite schema name: {schema!r}")
    return schema


def store_column(name: str) -> str:
    """Check a column name read from a store's schema; a strange one means a broken store."""

    if _IDENTIFIER.match(name) is None:
        raise MergeError(f"store column name is not a plain identifier: {name!r}")
    return name


@dataclass(frozen=True, slots=True)
class TableMerge:
    table: str
    source_rows: int
    target_rows_before: int
    inserted: int
    skipped: int
    target_rows_after: int


@dataclass(frozen=True, slots=True)
class MergeReport:
    tables: tuple[TableMerge, ...]

    @property
    def inserted(self) -> int:
        return sum(item.inserted for item in self.tables)

    @property
    def skipped(self) -> int:
        return sum(item.skipped for item in self.tables)

    def notes(self) -> tuple[str, ...]:
        """Lines a particular store adds under the table; none by default."""

        return ()

    def render(self) -> str:
        # The name column is sized to the longest table so a store with long names does
        # not run its counts together with them.
        width = max((len(item.table) for item in self.tables), default=5) + 2
        header = (
            f"{'table':{width}}{'source':>12}{'target':>12}"
            f"{'inserted':>10}{'skipped':>9}{'result':>12}"
        )
        lines = [header]
        for item in self.tables:
            lines.append(
                f"{item.table:{width}}{item.source_rows:>12}{item.target_rows_before:>12}"
                f"{item.inserted:>10}{item.skipped:>9}{item.target_rows_after:>12}"
            )
        lines.extend(self.notes())
        return "\n".join(lines)


def columns(connection: sqlite3.Connection, table: str, *, schema: str) -> Sequence[str]:
    statement = f'PRAGMA {schema_name(schema)}.table_info("{internal_name(table)}")'
    return [store_column(str(row[1])) for row in connection.execute(statement)]


def count(connection: sqlite3.Connection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    if row is None:
        raise MergeError(f"query returned no row: {sql}")
    return int(row[0])


def require_identical_columns(connection: sqlite3.Connection, tables: Sequence[str]) -> None:
    """The merge selects whole rows, so column order must match on both sides."""

    for table in tables:
        if columns(connection, table, schema="main") != columns(connection, table, schema="source"):
            raise MergeError(f"stores disagree on the columns of {table}")


def merge_table(
    connection: sqlite3.Connection,
    table: str,
    *,
    eligible: RowFilter = EVERY_ROW,
) -> TableMerge:
    name = internal_name(table)
    source_rows = count(connection, f'SELECT count(*) FROM source."{name}"')  # nosec B608
    before = count(connection, f'SELECT count(*) FROM main."{name}"')  # nosec B608
    skipped = count(
        connection,
        f'SELECT count(*) FROM source."{name}" WHERE NOT {eligible.unaliased}',  # nosec B608
    )
    names = columns(connection, table, schema="main")
    target_columns = ", ".join(f'"{column}"' for column in names)
    source_columns = ", ".join(f'source."{name}"."{column}"' for column in names)
    connection.execute(
        f'INSERT OR IGNORE INTO main."{name}" ({target_columns}) '  # nosec B608
        f'SELECT {source_columns} FROM source."{name}" WHERE {eligible.unaliased}'
    )
    after = count(connection, f'SELECT count(*) FROM main."{name}"')  # nosec B608
    return TableMerge(
        table=table,
        source_rows=source_rows,
        target_rows_before=before,
        inserted=after - before,
        skipped=skipped,
        target_rows_after=after,
    )


def require_matching_payloads(
    connection: sqlite3.Connection,
    table: str,
    *,
    keys: Sequence[str],
    eligible: RowFilter = EVERY_ROW,
    uncompared: Sequence[str] = (),
) -> None:
    """Refuse a shared key whose two copies disagree, rather than picking a winner.

    ``uncompared`` names columns that describe a store's own reading of the source
    rather than the source itself. Two stores that read the same record at different
    moments differ on those by construction, so comparing them would refuse every merge;
    the insert leaves the target's reading in place.
    """

    name = internal_name(table)
    names = columns(connection, table, schema="main")
    key_names = tuple(internal_name(key) for key in keys)
    skipped = {internal_name(column) for column in uncompared}
    payload = tuple(column for column in names if column not in key_names and column not in skipped)
    key_match = " AND ".join(f't."{key}" = s."{key}"' for key in key_names)
    if not payload:
        # A table that is all key has nothing to disagree about.
        return
    payload_differs = " OR ".join(f't."{column}" IS NOT s."{column}"' for column in payload)
    selected = ", ".join(f's."{key}"' for key in key_names)
    row = connection.execute(
        f"SELECT {selected} "  # nosec B608
        f'FROM source."{name}" s '
        f'JOIN main."{name}" t ON {key_match} '
        f"WHERE {eligible.aliased} "
        f"AND ({payload_differs}) "
        f"ORDER BY {selected} LIMIT 1"
    ).fetchone()
    if row is not None:
        raise MergeError(
            f"{table} payload disagrees for shared key: " + ", ".join(repr(value) for value in row)
        )


def source_only_rows(
    connection: sqlite3.Connection,
    table: str,
    *,
    keys: Sequence[str],
    eligible: RowFilter = EVERY_ROW,
) -> int:
    name = internal_name(table)
    match = " AND ".join(
        f'main."{name}"."{key}" = source."{name}"."{key}"' for key in map(internal_name, keys)
    )
    return count(
        connection,
        f'SELECT count(*) FROM source."{name}" '  # nosec B608
        f"WHERE {eligible.unaliased} AND NOT EXISTS ("
        f'SELECT 1 FROM main."{name}" WHERE {match})',
    )


def merge_fact_tables(
    connection: sqlite3.Connection,
    fact_keys: Mapping[str, tuple[str, ...]],
    *,
    eligible: RowFilter = EVERY_ROW,
    uncompared: Mapping[str, tuple[str, ...]] | None = None,
) -> tuple[TableMerge, ...]:
    """Merge every fact table, proving agreement before and after and losing no row.

    The payload check runs twice on purpose. Before the merge it rejects two stores that
    already disagree; after it, it catches a row the insert matched to a different one
    than the key comparison did, which a key the store does not actually enforce would
    produce silently.
    """

    exempt = uncompared or {}
    for table in fact_keys:
        require_matching_payloads(
            connection,
            table,
            keys=fact_keys[table],
            eligible=eligible,
            uncompared=exempt.get(table, ()),
        )
    merged = tuple(merge_table(connection, table, eligible=eligible) for table in fact_keys)
    for table in fact_keys:
        require_matching_payloads(
            connection,
            table,
            keys=fact_keys[table],
            eligible=eligible,
            uncompared=exempt.get(table, ()),
        )
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise MergeError(f"merge would break foreign keys: {violations[:3]}")
    left_behind = {
        table: remaining
        for table in fact_keys
        if (
            remaining := source_only_rows(
                connection, table, keys=fact_keys[table], eligible=eligible
            )
        )
    }
    if left_behind:
        raise MergeError(f"source rows are still missing after the merge: {left_behind}")
    return merged


__all__ = [
    "EVERY_ROW",
    "MergeError",
    "MergeReport",
    "RowFilter",
    "TableMerge",
    "columns",
    "count",
    "internal_name",
    "merge_fact_tables",
    "merge_table",
    "require_identical_columns",
    "require_matching_payloads",
    "schema_name",
    "source_only_rows",
    "store_column",
]
