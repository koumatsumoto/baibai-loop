"""Read-only validation of the macro stores against the current registry.

Macro state lives in two stores and only a local checkout holds both: the L1 indicator
store carries observations and their vintages, the application store carries the
published context reports written against them. Their agreement is what CI cannot see —
a workflow has no application store — so the pre-push instrument for it lives here.

The two directions of drift are asymmetric. An observation that the registry no longer
covers is a contract violation and fails. A published report is immutable: it must still
*load*, but citing a series that has since been retired is a warning, because retiring a
series is normal operation and rewriting history is not an option.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from baibai_engine.batch_api import (
    DEFAULT_MACRO_DB_PATH as DEFAULT_DB_PATH,
)
from baibai_engine.batch_api import (
    MACRO_CONTEXT_SCHEMA_VERSION,
    IndicatorDefinitions,
    IndicatorsSchemaError,
    MacroContextDocument,
    cited_series_ids,
    connect_read_only,
    database_path,
    load_definitions,
    scorecard_series_ids,
    validate_macro_schema,
)

MAX_RENDERED_VIOLATIONS = 100


@dataclass(frozen=True, slots=True)
class ValidationReport:
    schema_version: int
    registry_series: int
    observations: int
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
    *,
    definitions: IndicatorDefinitions | None = None,
) -> ValidationReport:
    """Check every stored vintage without opening the database for writes."""

    resolved = definitions or load_definitions()
    registry = resolved.by_id()
    violations: list[str] = []
    violation_count = 0

    def record(message: str) -> None:
        nonlocal violation_count
        violation_count += 1
        if len(violations) < MAX_RENDERED_VIOLATIONS:
            violations.append(message)

    for series in resolved.series:
        if series.plausible_min is None or series.plausible_max is None:
            record(f"{series.series_id}: registry plausible range is incomplete")

    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        validate_macro_schema(connection)
        observations = 0
        for row in connection.execute(
            # A withdrawn row is not a claim about a value, so the registry band does not
            # apply to it. Checking it anyway would make "retract an outlier, then tighten
            # the band" impossible: the retraction itself would fail the scan, and it
            # cannot be deleted because the merge restores it.
            "SELECT series_id, observed_at, vintage_at, value, unit "
            "FROM observations WHERE fetch_status != 'retracted' "
            "ORDER BY series_id, observed_at, vintage_at"
        ):
            observations += 1
            series_id = str(row["series_id"])
            definition = registry.get(series_id)
            identity = f"{series_id} {row['observed_at']} vintage {row['vintage_at']}"
            if definition is None:
                record(f"{identity}: series is absent from the current registry")
                continue
            if str(row["unit"]) != definition.unit:
                record(f"{identity}: unit {row['unit']!r}; expected {definition.unit!r}")
                continue
            value = float(row["value"])
            if not math.isfinite(value):
                record(f"{identity}: value {value!r} is not finite")
                continue
            low = definition.plausible_min
            high = definition.plausible_max
            if (low is not None and value < low) or (high is not None and value > high):
                rendered_low = "-inf" if low is None else f"{low:g}"
                rendered_high = "inf" if high is None else f"{high:g}"
                record(
                    f"{identity}: value {value:g} outside plausible range "
                    f"[{rendered_low}, {rendered_high}]"
                )
    finally:
        connection.close()

    return ValidationReport(
        schema_version=schema_version,
        registry_series=len(resolved.series),
        observations=observations,
        violation_count=violation_count,
        violations=tuple(violations),
    )


def validate_published_contexts(
    database: Path,
    *,
    definitions: IndicatorDefinitions | None = None,
) -> PublishedContextReport:
    """Load every current-contract report the way its consumers do, then look for drift.

    Loading is the forward instrument: `screening review-set publish` and the scorecard read reports
    through exactly this path, so a report that fails here is a report the daily batch
    cannot use. Registry drift is reported next to it because a retired series makes a
    scorecard unsettleable long before anyone notices from the report itself.
    """

    registry = frozenset(series.series_id for series in (definitions or load_definitions()).series)
    failures: list[str] = []
    warnings: list[str] = []
    connection = connect_read_only(database)
    try:
        rows = connection.execute(
            "SELECT context_id, payload FROM macro_context WHERE schema_version = ? "
            "ORDER BY published_at, context_id",
            (MACRO_CONTEXT_SCHEMA_VERSION,),
        ).fetchall()
    finally:
        connection.close()

    for row in rows:
        context_id = str(row["context_id"])
        try:
            document = MacroContextDocument.model_validate_json(str(row["payload"]))
        except ValidationError as exc:
            first = exc.errors()[0]
            location = ".".join(str(part) for part in first["loc"]) or "root"
            failures.append(f"{context_id}: cannot be read at {location}: {first['msg']}")
            continue
        retired = sorted(cited_series_ids(document) - registry)
        if retired:
            warnings.append(f"{context_id}: cites retired series: {', '.join(retired)}")
        unsettleable = sorted(scorecard_series_ids(document) - registry)
        if unsettleable:
            warnings.append(
                f"{context_id}: scorecard is unsettleable on retired series "
                f"({', '.join(unsettleable)})"
            )
    return PublishedContextReport(
        documents=len(rows),
        failures=tuple(failures),
        warnings=tuple(warnings),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "validate every macro observation against the current registry, "
            "and every published macro context report against the read path"
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--app-db", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = validate_store(args.db)
    except (IndicatorsSchemaError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: unable to validate indicator store {args.db}: {exc}", file=sys.stderr)
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
            f"error: unable to read published macro contexts {application_db}: {exc}",
            file=sys.stderr,
        )
        return 1
    return max(exit_code, _render_context_report(contexts))


def _render_store_report(report: ValidationReport) -> int:
    if report.valid:
        print(
            f"ok: schema v{report.schema_version}; {report.registry_series} registry series; "
            f"{report.observations} observations",
            flush=True,
        )
        return 0
    print(
        f"error: {report.violation_count} indicator store contract violations "
        f"across {report.observations} observations",
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
        print(f"ok: {report.documents} published macro context revisions read", flush=True)
        return 0
    print(
        f"error: {len(report.failures)} of {report.documents} published macro context "
        "revisions cannot be read",
        file=sys.stderr,
    )
    for failure in report.failures:
        print(f"- {failure}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
