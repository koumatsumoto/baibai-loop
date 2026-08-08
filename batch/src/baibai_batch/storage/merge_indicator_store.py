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

Facts are keyed, so the merge is an ``INSERT OR IGNORE`` per table after proving that every
shared key has the same full payload. A row only the source has is then added verbatim with
its vintage. Both stores must be on the schema this code writes; the transfer script moves
the downloaded copy there before the merge sees it.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from baibai_batch.storage.store_merge import (
    MergeError,
    MergeReport,
    RowFilter,
    merge_fact_tables,
    require_identical_columns,
)
from baibai_batch.storage.store_merge import (
    count as _count,
)
from baibai_batch.storage.store_merge import (
    internal_name as _internal_name,
)
from baibai_batch.storage.store_merge import (
    schema_name as _schema_name,
)
from baibai_engine.batch_api import (
    MACRO_SCHEMA_VERSION as SQLITE_SCHEMA_VERSION,
)
from baibai_engine.batch_api import (
    IndicatorsSchemaError,
)
from baibai_engine.batch_api import (
    validate_macro_schema as validate_current_schema,
)

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
_REGISTERED = RowFilter(
    unaliased='series_id IN (SELECT series_id FROM main."series")',
    aliased='s.series_id IN (SELECT series_id FROM main."series")',
)


@dataclass(frozen=True, slots=True)
class IndicatorMergeReport(MergeReport):
    """The merge result, plus the series whose facts the target no longer defines."""

    retired_series: tuple[str, ...] = ()

    def notes(self) -> tuple[str, ...]:
        if not self.retired_series:
            return ()
        return (
            "skipped rows belong to series the registry no longer defines: "
            + ", ".join(self.retired_series),
        )


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
            require_identical_columns(connection, (*FACT_KEYS, *REGISTRY_TABLES))
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
                missing = _source_series_missing_from_target(connection)
                if missing and target_generation == source_generation:
                    raise MergeError(
                        "source registry contains series missing from same-generation target: "
                        + ", ".join(missing)
                    )
                _validate_target_registry_contracts(connection)
                _validate_observations(connection, schema="main")
                _validate_observations(connection, schema="source")
                retired = _retired_series(connection)
                tables = merge_fact_tables(connection, FACT_KEYS, eligible=_REGISTERED)
                connection.commit()
                return IndicatorMergeReport(tables=tables, retired_series=retired)
            except BaseException:
                connection.rollback()
                raise
        finally:
            connection.execute("DETACH DATABASE source")


def _validate_target_registry_contracts(connection: sqlite3.Connection) -> None:
    """Require a complete finite band for every series retained by the target."""

    rows = connection.execute(
        """
        SELECT series_id, plausible_min, plausible_max
        FROM main.series
        ORDER BY series_id
        """
    )
    for series_id, low, high in rows:
        if low is None or high is None:
            raise MergeError(f"target series {series_id} has an incomplete plausible range")
        numeric_low = float(low)
        numeric_high = float(high)
        if (
            not math.isfinite(numeric_low)
            or not math.isfinite(numeric_high)
            or numeric_low > numeric_high
        ):
            raise MergeError(
                f"target series {series_id} has invalid plausible range "
                f"[{numeric_low!r}, {numeric_high!r}]"
            )


def _validate_observations(
    connection: sqlite3.Connection,
    *,
    schema: str,
) -> None:
    """Reject either store's facts when they violate the target contract."""

    # The statement below reads `attached`, never the argument, so deleting this line breaks
    # the query rather than quietly letting an unchecked name through.
    attached = _schema_name(schema)
    # A withdrawn row carries the value it withdrew, not a claim about one, so the
    # registry band does not apply to it on either side of the merge.
    registered = (
        'o.series_id IN (SELECT series_id FROM main."series")' if attached == "source" else "1"
    ) + " AND o.fetch_status != 'retracted'"
    row = connection.execute(
        f"""
        SELECT o.series_id, o.observed_at, o.value, o.unit,
               s.unit, s.plausible_min, s.plausible_max
        FROM {attached}.observations o
        JOIN main.series s ON s.series_id = o.series_id
        WHERE {registered}
          AND (o.unit != s.unit
           OR (s.plausible_min IS NOT NULL AND o.value < s.plausible_min)
           OR (s.plausible_max IS NOT NULL AND o.value > s.plausible_max))
        ORDER BY o.series_id, o.observed_at, o.vintage_at
        LIMIT 1
        """  # nosec B608
    ).fetchone()
    if row is None:
        return
    series_id, observed_at, value, actual_unit, expected_unit, low, high = row
    if str(actual_unit) != str(expected_unit):
        raise MergeError(
            f"{schema} observation {series_id} {observed_at} has unit "
            f"{actual_unit!r}; target registry expects {expected_unit!r}"
        )
    rendered_low = "-inf" if low is None else f"{float(low):g}"
    rendered_high = "inf" if high is None else f"{float(high):g}"
    raise MergeError(
        f"{schema} observation {series_id} {observed_at} value {float(value):g} "
        f"is outside target plausible range [{rendered_low}, {rendered_high}]"
    )


def _retired_series(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Series the source carries facts for that the target's registry does not define."""

    union = " UNION ".join(
        f'SELECT DISTINCT series_id FROM source."{table}" '  # nosec B608
        f"WHERE NOT {_REGISTERED.unaliased}"
        for table in map(_internal_name, FACT_KEYS)
    )
    return tuple(str(row[0]) for row in connection.execute(f"{union} ORDER BY series_id"))


def _source_series_missing_from_target(connection: sqlite3.Connection) -> tuple[str, ...]:
    """Registry members the same-generation target must retain, even before first fetch."""

    return tuple(
        str(row[0])
        for row in connection.execute(
            """
            SELECT source.series.series_id
            FROM source.series
            LEFT JOIN main.series
              ON main.series.series_id = source.series.series_id
            WHERE main.series.series_id IS NULL
            ORDER BY source.series.series_id
            """
        )
    )


def _require_schema(
    connection: sqlite3.Connection,
    *,
    path: Path,
    schema: str = "main",
) -> int:
    version = _count(connection, f"PRAGMA {_schema_name(schema)}.user_version")
    if version != SQLITE_SCHEMA_VERSION:
        raise MergeError(
            f"indicator store schema is {version} but this code expects "
            f"{SQLITE_SCHEMA_VERSION}: {path}"
        )
    try:
        validate_current_schema(connection, schema=schema)
    except IndicatorsSchemaError as error:
        raise MergeError(f"indicator store schema contract is invalid: {path}: {error}") from error
    return version


def _registry_generation(connection: sqlite3.Connection, *, schema: str) -> int:
    return _count(
        connection,
        f'SELECT generation FROM {_schema_name(schema)}."registry_state" '  # nosec B608
        "WHERE singleton = 1",
    )


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
