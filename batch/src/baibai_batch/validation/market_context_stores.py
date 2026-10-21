"""Read-only validation of collected sources and saved market assessments.

The source store retains uninterpreted inputs and acquisition outcomes. The
application store carries immutable published context reports. This check
validates each store on its own terms without treating source payloads as L2
observations or rewriting reports that cite a retired series.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from baibai_engine.batch_api import (
    DEFAULT_ECONOMIC_DB_PATH as DEFAULT_DB_PATH,
)
from baibai_engine.batch_api import (
    EconomicSchemaError,
    IndicatorDefinitions,
    StoredAssessment,
    assessment_series_ids,
    connect_read_only,
    database_path,
    decode_economic_source_records,
    load_definitions,
    validate_economic_schema,
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


@dataclass(frozen=True, slots=True)
class PublishedContextReport:
    documents: int
    failures: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return not self.failures


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


def validate_published_contexts(
    database: Path,
    *,
    definitions: IndicatorDefinitions | None = None,
) -> PublishedContextReport:
    """Load every current-contract report the way its consumers do, then look for drift.

    Detect reports that current Context consumers can no longer load. candidate list publication
    does not consume Context. A retired series may leave a report readable but its scorecard
    unsettleable, so registry drift is reported separately.
    """

    registry = frozenset(series.series_id for series in (definitions or load_definitions()).series)
    failures: list[str] = []
    warnings: list[str] = []
    connection = connect_read_only(database)
    try:
        rows = connection.execute(
            (
                "SELECT context_id, schema_version, as_of, published_at, payload "
                "FROM market_context_assessment "
                "ORDER BY published_at, context_id"
            ),
        ).fetchall()
    finally:
        connection.close()

    for row in rows:
        context_id = str(row["context_id"])
        try:
            document = StoredAssessment.from_payload(json.loads(str(row["payload"])))
            if (
                document.context_id != context_id
                or document.schema_version != row["schema_version"]
                or document.as_of.isoformat() != row["as_of"]
            ):
                raise ValueError("publication identity differs")
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            failures.append(f"{context_id}: cannot be read: {exc}")
            continue
        retired = sorted(assessment_series_ids(document) - registry)
        if retired:
            warnings.append(f"{context_id}: cites retired series: {', '.join(retired)}")

    return PublishedContextReport(
        documents=len(rows),
        failures=tuple(failures),
        warnings=tuple(warnings),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "validate collected source payloads and every saved market assessment "
            "against the read path"
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--app-db", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = validate_store(args.db)
    except (EconomicSchemaError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: unable to validate economic source store {args.db}: {exc}", file=sys.stderr)
        return 1
    exit_code = _render_store_report(report)

    application_db = database_path(args.app_db)
    if not application_db.is_file():
        # A checkout without an application store has published nothing; absence is a
        # valid state, not a missing report.
        print(f"skip: no application store at {application_db}", flush=True)
        return exit_code
    try:
        contexts = validate_published_contexts(application_db)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(
            f"error: unable to read published market context assessments {application_db}: {exc}",
            file=sys.stderr,
        )
        return 1
    return max(exit_code, _render_context_report(contexts))


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


def _render_context_report(report: PublishedContextReport) -> int:
    for warning in report.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if report.valid:
        print(
            f"ok: {report.documents} published market context assessment revisions read", flush=True
        )
        return 0
    print(
        f"error: {len(report.failures)} of {report.documents} published market context assessment "
        "revisions cannot be read",
        file=sys.stderr,
    )
    for failure in report.failures:
        print(f"- {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
