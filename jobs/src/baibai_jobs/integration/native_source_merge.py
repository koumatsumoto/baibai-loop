"""Merge source receipts without importing L2 definitions or computed histories."""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

from baibai_engine.data_collection.acquisition.refresh import SourceRefreshSuccess, refresh_sources
from baibai_engine.data_collection.catalog import load_sources
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
    save_collection_confirmation,
    save_confirmation,
    validate_collection_confirmations,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.records import decode_records
from baibai_engine.data_collection.receipts.native_sources.store import (
    RECEIPT_KEYS as ECONOMIC_RECEIPT_KEYS,
)
from baibai_engine.data_collection.receipts.native_sources.store import (
    EconomicSchemaError,
    validate_current_schema,
)
from baibai_engine.storage.sqlite.merge import (
    MergeError,
    MergeReport,
    RowFilter,
    merge_fact_tables,
    require_identical_columns,
    require_matching_payloads,
    source_only_rows,
)


def verify_stores(source: Path, target: Path) -> None:
    """Require every original receipt in readback without changing either file."""
    with closing(sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        validate_current_schema(conn)
        conn.execute("ATTACH DATABASE ? AS source", (source.resolve().as_uri() + "?mode=ro",))
        try:
            validate_current_schema(conn, schema="source")
            require_identical_columns(conn, tuple(ECONOMIC_RECEIPT_KEYS))
            for table, keys in ECONOMIC_RECEIPT_KEYS.items():
                require_matching_payloads(conn, table, keys=keys)
                if source_only_rows(conn, table, keys=keys):
                    raise MergeError("economic publication readback is missing received rows")
            for row in conn.execute("SELECT * FROM source.economic_source_record_confirmations"):
                actual = conn.execute(
                    "SELECT * FROM economic_source_record_confirmations "
                    "WHERE source_id=? AND observed_at=?",
                    tuple(row[:2]),
                ).fetchone()
                if (
                    actual is None
                    or actual[2] < row[2]
                    or (actual[2] == row[2] and tuple(actual) != tuple(row))
                ):
                    raise MergeError(
                        "economic publication readback is missing its source confirmation"
                    )
            for row in conn.execute(
                "SELECT * FROM source.economic_source_collection_confirmations"
            ):
                actual = conn.execute(
                    "SELECT * FROM economic_source_collection_confirmations WHERE source_id=?",
                    row[:1],
                ).fetchone()
                if (
                    actual is None
                    or actual[1] < row[1]
                    or (actual[1] == row[1] and tuple(actual) != tuple(row))
                ):
                    raise MergeError(
                        "economic publication readback is missing its complete confirmation"
                    )
        finally:
            conn.execute("DETACH DATABASE source")


def merge_stores(source: Path, target: Path) -> MergeReport:
    for path in (source, target):
        if not path.is_file():
            raise MergeError(f"economic source store does not exist: {path}")
    with closing(
        sqlite3.connect(target.resolve().as_uri(), uri=True, isolation_level=None)
    ) as connection:
        try:
            validate_current_schema(connection)
            connection.execute(
                "ATTACH DATABASE ? AS source", (f"{source.resolve().as_uri()}?mode=ro",)
            )
            try:
                validate_current_schema(connection, schema="source")
                require_identical_columns(connection, tuple(ECONOMIC_RECEIPT_KEYS))
                connection.execute("BEGIN IMMEDIATE")
                try:
                    tables = merge_fact_tables(connection, ECONOMIC_RECEIPT_KEYS)
                    for row in connection.execute(
                        "SELECT * FROM source.economic_source_record_confirmations"
                    ):
                        try:
                            save_confirmation(connection, tuple(row))
                        except ValueError as error:
                            raise MergeError(str(error)) from error
                    for row in connection.execute(
                        "SELECT * FROM source.economic_source_collection_confirmations"
                    ):
                        try:
                            save_collection_confirmation(connection, tuple(row))
                        except ValueError as error:
                            raise MergeError(str(error)) from error
                    try:
                        validate_collection_confirmations(connection)
                    except ValueError as error:
                        raise MergeError(str(error)) from error
                    connection.commit()
                    return MergeReport(tables=tables)
                except BaseException:
                    connection.rollback()
                    raise
            finally:
                connection.execute("DETACH DATABASE source")
        except EconomicSchemaError as error:
            raise MergeError(str(error)) from error


def reconcile_stores(
    canonical: Path, target: Path, *, recheck_sources: tuple[str, ...] = ()
) -> MergeReport:
    """Accept local receipts against fresh canonical before preparing publication.

    Keep every original row in both copies. Only the mutable confirmation state
    is replaced with canonical state followed by accepted local confirmations.
    The write lock covers comparison and commit, so concurrent local acquisition
    cannot disappear while a publication candidate is prepared.
    """
    for path in (canonical, target):
        if not path.is_file():
            raise MergeError(f"economic source store does not exist: {path}")
    with (
        closing(
            sqlite3.connect(target.resolve().as_uri(), uri=True, isolation_level=None)
        ) as connection,
        tempfile.TemporaryDirectory(
            prefix=".economic-publication-", dir=target.parent
        ) as directory,
    ):
        try:
            validate_current_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                candidate = Path(directory) / "canonical.sqlite"
                accepted = Path(directory) / "accepted.sqlite"
                with (
                    closing(
                        sqlite3.connect(canonical.resolve().as_uri() + "?mode=ro", uri=True)
                    ) as source,
                    closing(sqlite3.connect(candidate)) as copy,
                ):
                    validate_current_schema(source)
                    source.backup(copy)
                received = target
                if recheck_sources:
                    _recheck_fresh_copy(candidate, target, recheck_sources)
                    # Rechecked sources already retain every local original in
                    # candidate. Do not reuse their lost/stale confirmation state.
                    received = Path(directory) / "remaining.sqlite"
                    other_sources = {
                        str(row[0])
                        for table in ECONOMIC_RECEIPT_KEYS
                        for row in connection.execute(f"SELECT DISTINCT source_id FROM {table}")  # nosec B608
                    } - set(recheck_sources)
                    export_receipts(
                        target,
                        received,
                        source_ids=sorted(other_sources),
                        since=datetime.min.replace(tzinfo=UTC),
                    )
                result = accept_rechecks(received, candidate, accepted)
                late = result["late_recovery_required"]
                assert isinstance(late, list)
                unresolved = []
                with (
                    closing(sqlite3.connect(accepted)) as selected,
                    closing(sqlite3.connect(candidate)) as current,
                ):
                    for receipt in late:
                        if receipt[1] == "*":
                            unresolved.append(receipt)
                            continue
                        confirmed = any(
                            database.execute(
                                "SELECT 1 FROM economic_source_record_confirmations "
                                "WHERE source_id=? AND observed_at=?",
                                receipt[:2],
                            ).fetchone()
                            for database in (connection, current)
                        )
                        if not confirmed:
                            # Other providers still use their original append
                            # union and latest-receipt reader, not FRED state.
                            continue
                        check = selected.execute(
                            "SELECT checked_at FROM economic_source_record_confirmations "
                            "WHERE source_id=? AND observed_at=?",
                            receipt[:2],
                        ).fetchone()
                        # A new successful recheck can settle current adoption
                        # while retaining the older unadopted original unchanged.
                        if check is None or check[0] <= receipt[2]:
                            unresolved.append(receipt)
                if unresolved:
                    raise MergeError(
                        f"economic publication requires explicit source recheck: {unresolved}"
                    )
                merge_stores(accepted, candidate)
                connection.execute(
                    "ATTACH DATABASE ? AS source", (candidate.resolve().as_uri() + "?mode=ro",)
                )
                require_identical_columns(connection, tuple(ECONOMIC_RECEIPT_KEYS))
                tables = merge_fact_tables(connection, ECONOMIC_RECEIPT_KEYS)
                connection.execute("DELETE FROM economic_source_record_confirmations")
                connection.execute("DELETE FROM economic_source_collection_confirmations")
                for row in connection.execute(
                    "SELECT * FROM source.economic_source_record_confirmations"
                ):
                    save_confirmation(connection, tuple(row))
                for row in connection.execute(
                    "SELECT * FROM source.economic_source_collection_confirmations"
                ):
                    save_collection_confirmation(connection, tuple(row))
                validate_collection_confirmations(connection)
                connection.commit()
                return MergeReport(tables=tables)
            except BaseException:
                connection.rollback()
                raise
        except (EconomicSchemaError, ValueError) as error:
            raise MergeError(str(error)) from error


def _recheck_fresh_copy(candidate: Path, received: Path, source_ids: tuple[str, ...]) -> None:
    """Recheck fresh canonical, then retain local originals against that same response."""
    catalog = load_sources()
    if any(
        source_id not in catalog or catalog[source_id].provider != "fred_csv"
        for source_id in source_ids
    ):
        raise ValueError("explicit confirmation recovery requires named FRED sources")
    # Compare the actual response with fresh canonical BEFORE local originals
    # are preserved. A late local body must never become the adopted baseline.
    outcomes = refresh_sources(
        source_ids, db_path=candidate, start=None, end=datetime.now(UTC).date(), recent=True
    )
    if not all(isinstance(outcome, SourceRefreshSuccess) for outcome in outcomes):
        raise MergeError("explicit FRED recovery failed; original copies were not changed")
    with (
        closing(sqlite3.connect(candidate.resolve().as_uri(), uri=True)) as connection,
        closing(sqlite3.connect(received.resolve().as_uri() + "?mode=ro", uri=True)) as raw,
    ):
        validate_collection_confirmations(connection)
        responses = []
        for source_id in source_ids:
            checked = connection.execute(
                "SELECT checked_at FROM economic_source_collection_confirmations WHERE source_id=?",
                (source_id,),
            ).fetchone()
            old = raw.execute(
                "SELECT MAX(checked_at) FROM economic_source_record_confirmations "
                "WHERE source_id=?",
                (source_id,),
            ).fetchone()
            if checked is None or (old[0] is not None and checked[0] <= old[0]):
                raise MergeError(
                    "explicit FRED recovery did not establish a newer real confirmation"
                )
            records = {
                date.fromisoformat(day): decode_records(payload)
                for day, payload in connection.execute(
                    "SELECT r.observed_at,r.payload FROM economic_source_record_confirmations c "
                    "JOIN economic_source_records r ON r.source_id=c.source_id "
                    "AND r.observed_at=c.observed_at AND r.fetched_at=c.adopted_fetched_at "
                    "WHERE c.source_id=? AND r.record_count>0",
                    (source_id,),
                )
            }
            responses.append((source_id, datetime.fromisoformat(checked[0]), records))
        connection.execute(
            "ATTACH DATABASE ? AS source", (received.resolve().as_uri() + "?mode=ro",)
        )
        names = ",".join(
            connection.execute("SELECT quote(?)", (name,)).fetchone()[0] for name in source_ids
        )
        with connection:
            merge_fact_tables(
                connection,
                ECONOMIC_RECEIPT_KEYS,
                eligible=RowFilter(f"source_id IN ({names})", f"s.source_id IN ({names})"),
            )
            # Reuse the exact completed response and its clock, not another fetch,
            # run or count. Only local-only real periods need absence confirmation.
            for source_id, checked_at, records_by_day in responses:
                confirm_source_response(
                    connection,
                    source_id=source_id,
                    fetched_at=checked_at,
                    records_by_day=records_by_day,
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--recheck-source", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        report = reconcile_stores(
            args.canonical, args.target, recheck_sources=tuple(args.recheck_source)
        )
    except MergeError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(report.render())
    print(f"merged {report.inserted} rows into {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
