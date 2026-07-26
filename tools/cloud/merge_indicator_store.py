"""Merge one indicator store into another, keeping every observation both sides hold.

The indicator store has two writers: the daily batch refreshes a rolling window in the
cloud, and an operator deepens history locally with ``macro refresh --all-history``. Each
therefore holds observations the other has never seen, so publishing the local store means
merging the cloud copy into it first and proving that nothing cloud-side is left behind.

Only the tables that accumulate facts are merged. ``series`` and ``aliases`` are owned by the
target store and are not imported from the source. Ordinary opens preserve metadata for series
that a stale branch does not know, so cloud facts for those retained series remain eligible for
merge. A target with an older registry generation is rejected before merge. A same-generation
target missing source series is also rejected as incomplete. Only a strictly newer target may
report absent source series as skipped retirement instead of reviving them.

Facts are keyed, so the merge is an ``INSERT OR IGNORE`` per table: a row the target already
has keeps the target's version, and a row only the source has is added verbatim with its
vintage.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.macro.indicators.db import SQLITE_SCHEMA_VERSION

# The tables that accumulate facts, with the key that decides whether a row is the same row.
FACT_KEYS: Mapping[str, tuple[str, ...]] = {
    "observations": ("series_id", "observed_at", "vintage_at"),
    "provider_runs": ("run_id",),
}
# The tables the registry owns: the target's version is the only one.
REGISTRY_TABLES: tuple[str, ...] = (
    "series",
    "aliases",
    "registry_state",
    "registry_prune_authorizations",
)

# A row is only carried when the target store retains its series metadata. Only
# an explicit refresh may remove that metadata and make the series ineligible.
_REGISTERED = 'series_id IN (SELECT series_id FROM main."series")'


class MergeError(RuntimeError):
    """The stores cannot be merged, so the target is left untouched."""


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
    retired_series: tuple[str, ...] = ()

    @property
    def inserted(self) -> int:
        return sum(item.inserted for item in self.tables)

    @property
    def skipped(self) -> int:
        return sum(item.skipped for item in self.tables)

    def render(self) -> str:
        lines = [
            f"{'table':16}{'source':>10}{'target':>10}{'inserted':>10}{'skipped':>9}{'result':>10}"
        ]
        for item in self.tables:
            lines.append(
                f"{item.table:16}{item.source_rows:>10}{item.target_rows_before:>10}"
                f"{item.inserted:>10}{item.skipped:>9}{item.target_rows_after:>10}"
            )
        if self.retired_series:
            lines.append(
                "skipped rows belong to series the registry no longer defines: "
                + ", ".join(self.retired_series)
            )
        return "\n".join(lines)


def merge_stores(source: Path, target: Path) -> MergeReport:
    """Insert every source fact the target lacks, and verify none is left behind."""

    for path in (source, target):
        if not path.is_file():
            raise MergeError(f"indicator store does not exist: {path}")
    uri = f"{target.resolve().as_uri()}"
    with closing(sqlite3.connect(uri, uri=True, isolation_level=None)) as connection:
        _require_schema(connection, path=target)
        connection.execute("ATTACH DATABASE ? AS source", (f"{source.resolve().as_uri()}?mode=ro",))
        try:
            _require_schema(connection, path=source, schema="source")
            _require_identical_columns(connection)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                target_generation = _registry_generation(connection, schema="main")
                source_generation = _registry_generation(connection, schema="source")
                if target_generation < source_generation:
                    raise MergeError(
                        "target registry generation is older than source; "
                        f"target={target_generation}, source={source_generation}"
                    )
                retired = _retired_series(connection)
                if retired and target_generation == source_generation:
                    raise MergeError(
                        "source facts belong to series missing from same-generation target: "
                        + ", ".join(retired)
                    )
                tables = tuple(_merge_table(connection, table) for table in FACT_KEYS)
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise MergeError(f"merge would break foreign keys: {violations[:3]}")
                left_behind = {
                    table: remaining
                    for table in FACT_KEYS
                    if (remaining := _source_only_rows(connection, table))
                }
                if left_behind:
                    raise MergeError(
                        f"source rows are still missing after the merge: {left_behind}"
                    )
                connection.commit()
                return MergeReport(tables=tables, retired_series=retired)
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.execute("DETACH DATABASE source")


def _merge_table(connection: sqlite3.Connection, table: str) -> TableMerge:
    source_rows = _count(connection, f'SELECT count(*) FROM source."{table}"')
    before = _count(connection, f'SELECT count(*) FROM main."{table}"')
    skipped = _count(connection, f'SELECT count(*) FROM source."{table}" WHERE NOT {_REGISTERED}')
    connection.execute(
        f'INSERT OR IGNORE INTO main."{table}" SELECT * FROM source."{table}" WHERE {_REGISTERED}'
    )
    after = _count(connection, f'SELECT count(*) FROM main."{table}"')
    return TableMerge(
        table=table,
        source_rows=source_rows,
        target_rows_before=before,
        inserted=after - before,
        skipped=skipped,
        target_rows_after=after,
    )


def _source_only_rows(connection: sqlite3.Connection, table: str) -> int:
    match = " AND ".join(
        f'main."{table}".{key} = source."{table}".{key}' for key in FACT_KEYS[table]
    )
    return _count(
        connection,
        f'SELECT count(*) FROM source."{table}" WHERE {_REGISTERED} AND NOT EXISTS ('
        f'SELECT 1 FROM main."{table}" WHERE {match})',
    )


def _retired_series(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Series the source carries facts for that the target's registry does not define."""

    union = " UNION ".join(
        f'SELECT DISTINCT series_id FROM source."{table}" WHERE NOT {_REGISTERED}'
        for table in FACT_KEYS
    )
    return tuple(str(row[0]) for row in connection.execute(f"{union} ORDER BY series_id"))


def _require_schema(connection: sqlite3.Connection, *, path: Path, schema: str = "main") -> None:
    version = _count(connection, f"PRAGMA {schema}.user_version")
    if version != SQLITE_SCHEMA_VERSION:
        raise MergeError(
            f"indicator store schema is {version} but this code expects "
            f"{SQLITE_SCHEMA_VERSION}: {path}"
        )


def _require_identical_columns(connection: sqlite3.Connection) -> None:
    """The merge selects whole rows, so column order must match on both sides."""

    for table in (*FACT_KEYS, *REGISTRY_TABLES):
        if _columns(connection, table, schema="main") != _columns(
            connection, table, schema="source"
        ):
            raise MergeError(f"indicator stores disagree on the columns of {table}")


def _registry_generation(connection: sqlite3.Connection, *, schema: str) -> int:
    return _count(
        connection,
        f'SELECT generation FROM {schema}."registry_state" WHERE singleton = 1',
    )


def _columns(connection: sqlite3.Connection, table: str, *, schema: str) -> Sequence[str]:
    return [str(row[1]) for row in connection.execute(f'PRAGMA {schema}.table_info("{table}")')]


def _count(connection: sqlite3.Connection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    if row is None:
        raise MergeError(f"query returned no row: {sql}")
    return int(row[0])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="store to merge from")
    parser.add_argument("--target", type=Path, required=True, help="store to merge into")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = merge_stores(args.source, args.target)
    except MergeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(report.render())
    print(f"merged {report.inserted} rows into {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
