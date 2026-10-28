"""Read-only validation of collected economic source payloads and metadata."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.data_collection.receipts.native_sources.records import (
    decode_records as decode_economic_source_records,
)
from baibai_engine.data_collection.receipts.native_sources.store import (
    DEFAULT_DB_PATH,
    EconomicSchemaError,
)
from baibai_engine.data_collection.receipts.native_sources.store import (
    validate_current_schema as validate_economic_schema,
)

MAX_RENDERED_VIOLATIONS = 100


@dataclass(frozen=True, slots=True)
class ValidationReport:
    schema_version: int
    source_records: int
    source_documents: int
    source_runs: int
    violation_count: int
    violations: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return self.violation_count == 0


def validate_store(
    database: Path,
) -> ValidationReport:
    """Check source payload counts and document metadata without L2 interpretation."""
    violations: list[str] = []
    violation_count = 0

    def record(message: str) -> None:
        nonlocal violation_count
        violation_count += 1
        if len(violations) < MAX_RENDERED_VIOLATIONS:
            violations.append(message)

    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        validate_economic_schema(connection)
        source_records = 0
        for row in connection.execute(
            "SELECT source_id, observed_at, fetched_at, record_count, payload "
            "FROM economic_source_records ORDER BY source_id, observed_at, fetched_at"
        ):
            source_records += 1
            identity = f"{row['source_id']} {row['observed_at']} fetched {row['fetched_at']}"
            try:
                native = decode_economic_source_records(row["payload"])
            except (OSError, EOFError, ValueError, zlib.error):
                record(f"{identity}: native payload cannot be decoded")
                continue
            if len(native) != row["record_count"]:
                record(f"{identity}: native record count differs from the payload")
        source_documents = 0
        for row in connection.execute(
            "SELECT source_id, request_key, fetched_at, metadata "
            "FROM economic_source_documents ORDER BY source_id, request_key, fetched_at"
        ):
            source_documents += 1
            identity = f"{row['source_id']} {row['request_key']} fetched {row['fetched_at']}"
            try:
                metadata = json.loads(str(row["metadata"]))
            except (TypeError, ValueError):
                record(f"{identity}: document metadata is not a JSON object")
                continue
            if not isinstance(metadata, dict):
                record(f"{identity}: document metadata is not a JSON object")
        source_runs = int(
            connection.execute("SELECT COUNT(*) FROM economic_source_runs").fetchone()[0]
        )
    finally:
        connection.close()

    return ValidationReport(
        schema_version=schema_version,
        source_records=source_records,
        source_documents=source_documents,
        source_runs=source_runs,
        violation_count=violation_count,
        violations=tuple(violations),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="validate collected economic source payloads and record counts"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = validate_store(args.db)
    except (EconomicSchemaError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: unable to validate economic source store {args.db}: {exc}", file=sys.stderr)
        return 1
    return _render_store_report(report)


def _render_store_report(report: ValidationReport) -> int:
    if report.valid:
        print(
            f"ok: schema v{report.schema_version}; {report.source_records} native records; "
            f"{report.source_documents} documents; {report.source_runs} source runs",
            flush=True,
        )
        return 0
    print(
        f"error: {report.violation_count} economic source store contract violations "
        f"across {report.source_records} native records, "
        f"{report.source_documents} documents and {report.source_runs} source runs",
        file=sys.stderr,
    )
    for violation in report.violations:
        print(f"- {violation}", file=sys.stderr)
    if report.violation_count > len(report.violations):
        print(
            f"- ... {report.violation_count - len(report.violations)} more",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
