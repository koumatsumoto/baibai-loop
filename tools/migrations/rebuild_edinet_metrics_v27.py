"""Rebuild every saved edinet.metrics as-of through the current extractor.

Temporary one-shot tool for issue #1309. The source is opened read-only and the
destination must not exist. Every partition is rebuilt in chronological order so the
normal extractor can reuse an unchanged filing from the immediately preceding current-
revision partition. No field is patched by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

from baibai_engine.market.config import DEFAULT_CACHE_DIR
from baibai_engine.market.sqlite import SQLITE_SCHEMA_VERSION, validate_current_schema
from baibai_engine.screening.cli.edinet_extract import extract_edinet_metrics_command
from baibai_engine.screening.edinet_revision import compute_extractor_revision
from baibai_engine.screening.providers.edinet import EDINETProvider

PartitionRunner = Callable[[date, Path], int]
NONANNUAL_DETAIL_COUNT_SQL = """
SELECT COUNT(*)
FROM edinet_metrics
WHERE document_type NOT IN ('120', '130')
  AND (
    ocf_receivables_cash_effect IS NOT NULL
    OR ocf_inventories_cash_effect IS NOT NULL
    OR ocf_payables_cash_effect IS NOT NULL
    OR ocf_contract_liabilities_cash_effect IS NOT NULL
    OR ocf_advances_received_cash_effect IS NOT NULL
    OR ocf_other_payables_cash_effect IS NOT NULL
    OR capex_ppe_reported IS NOT NULL
    OR capex_intangible_reported IS NOT NULL
  )
"""


def _scalar(connection: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> object:
    row = connection.execute(sql, params).fetchone()
    if row is None:
        raise RuntimeError(f"query returned no row: {sql}")
    return row[0]


def _integer_scalar(
    connection: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()
) -> int:
    value = _scalar(connection, sql, params)
    if not isinstance(value, int):
        raise RuntimeError(f"query returned non-integer value: {sql}")
    return value


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    # Identifiers are sourced exclusively from sqlite_master in this connection.
    return {
        table: _integer_scalar(connection, f'SELECT COUNT(*) FROM "{table}"')  # nosec B608
        for table in tables
    }


def _metric_keys_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for asof_date, ticker in connection.execute(
        "SELECT asof_date, ticker FROM edinet_metrics ORDER BY asof_date, ticker"
    ):
        digest.update(f"{asof_date}\0{ticker}\n".encode())
    return digest.hexdigest()


def _snapshot(source: Path, building: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    target_connection = sqlite3.connect(building)
    try:
        validate_current_schema(source_connection)
        source_connection.backup(target_connection, pages=16_384)
    finally:
        target_connection.close()
        source_connection.close()


def _default_runner(sqlite_path: Path) -> PartitionRunner:
    provider = EDINETProvider(None, DEFAULT_CACHE_DIR, sqlite_path=sqlite_path, cache_only=True)

    def run(asof_date: date, target: Path) -> int:
        return extract_edinet_metrics_command(
            asof_date=asof_date,
            lookback_days=540,
            provider=provider,
            sqlite_path=target,
        )

    return run


def rebuild_edinet_metrics_v27(
    source: Path,
    destination: Path,
    *,
    run_partition: PartitionRunner | None = None,
) -> tuple[int, int]:
    source = source.resolve()
    destination = destination.resolve()
    building = destination.with_name(f".{destination.name}.building")
    if not source.is_file():
        raise FileNotFoundError(f"source does not exist: {source}")
    if destination.exists() or building.exists():
        raise FileExistsError(f"destination or staging file already exists: {destination}")

    try:
        _snapshot(source, building)
        connection = sqlite3.connect(building)
        try:
            validate_current_schema(connection)
            version = _integer_scalar(connection, "PRAGMA user_version")
            if version != SQLITE_SCHEMA_VERSION:
                raise RuntimeError(
                    f"source schema is {version}; expected current {SQLITE_SCHEMA_VERSION}"
                )
            before_counts = _table_counts(connection)
            before_keys = _metric_keys_digest(connection)
            asofs = [
                date.fromisoformat(str(row[0]))
                for row in connection.execute(
                    "SELECT DISTINCT asof_date FROM edinet_metrics ORDER BY asof_date"
                )
            ]
            expected_rows = {
                date.fromisoformat(str(row[0])): int(row[1])
                for row in connection.execute(
                    "SELECT asof_date, COUNT(*) FROM edinet_metrics GROUP BY asof_date"
                )
            }
        finally:
            connection.close()
        if not asofs:
            raise RuntimeError("edinet_metrics has no saved as-of partitions")

        runner = run_partition or _default_runner(building)
        for index, asof_date in enumerate(asofs, start=1):
            print(f"rebuilding edinet.metrics {index}/{len(asofs)}: {asof_date}", flush=True)
            connection = sqlite3.connect(building)
            try:
                deleted = connection.execute(
                    "DELETE FROM source_coverage WHERE source = 'edinet_metrics' "
                    "AND coverage_key = ?",
                    (asof_date.isoformat(),),
                ).rowcount
                if deleted != 1:
                    raise RuntimeError(
                        f"expected one coverage row for {asof_date}, deleted {deleted}"
                    )
                connection.commit()
            finally:
                connection.close()
            if runner(asof_date, building) != 0:
                raise RuntimeError(f"extractor failed for {asof_date}")

        connection = sqlite3.connect(building)
        try:
            validate_current_schema(connection)
            if str(_scalar(connection, "PRAGMA integrity_check")) != "ok":
                raise RuntimeError("rebuilt store failed integrity_check")
            foreign_key_errors = list(connection.execute("PRAGMA foreign_key_check"))
            if foreign_key_errors:
                raise RuntimeError(
                    f"rebuilt store failed foreign_key_check: {foreign_key_errors[:5]}"
                )
            after_counts = _table_counts(connection)
            if after_counts != before_counts:
                differences = {
                    key: (before_counts.get(key), after_counts.get(key))
                    for key in sorted(set(before_counts) | set(after_counts))
                    if before_counts.get(key) != after_counts.get(key)
                }
                raise RuntimeError(f"table counts changed: {differences}")
            if _metric_keys_digest(connection) != before_keys:
                raise RuntimeError("edinet_metrics primary-key population changed")
            actual_rows = {
                date.fromisoformat(str(row[0])): int(row[1])
                for row in connection.execute(
                    "SELECT asof_date, COUNT(*) FROM edinet_metrics GROUP BY asof_date"
                )
            }
            if actual_rows != expected_rows:
                raise RuntimeError("edinet_metrics per-asof row counts changed")
            revision = compute_extractor_revision()
            wrong_revision = _integer_scalar(
                connection,
                "SELECT COUNT(*) FROM edinet_metrics WHERE extractor_revision IS NOT ?",
                (revision,),
            )
            if wrong_revision:
                raise RuntimeError(f"{wrong_revision} rows do not carry current extractor revision")
            nonannual_details = _integer_scalar(
                connection,
                NONANNUAL_DETAIL_COUNT_SQL,
            )
            if nonannual_details:
                raise RuntimeError(f"{nonannual_details} nonannual rows contain CF details")
        finally:
            connection.close()
        building.replace(destination)
        return len(asofs), before_counts["edinet_metrics"]
    except BaseException:
        building.unlink(missing_ok=True)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    partitions, rows = rebuild_edinet_metrics_v27(args.source, args.destination)
    print(f"rebuilt {partitions} partition(s), {rows} row(s): {args.destination.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
