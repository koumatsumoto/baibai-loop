"""Read-only validation of a macro indicator store against the current registry."""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.macro.indicators.db import (
    DEFAULT_DB_PATH,
    SQLITE_SCHEMA_VERSION,
    IndicatorsSchemaError,
    validate_current_schema,
)
from baibai_engine.macro.indicators.definitions import (
    IndicatorDefinitions,
    load_definitions,
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
        if schema_version == SQLITE_SCHEMA_VERSION:
            validate_current_schema(connection)
        elif 1 <= schema_version < SQLITE_SCHEMA_VERSION:
            _validate_observation_read_capability(connection, schema_version)
        else:
            raise IndicatorsSchemaError(
                f"unsupported indicator SQLite schema: {schema_version}; "
                f"expected 1..{SQLITE_SCHEMA_VERSION}"
            )
        observations = 0
        for row in connection.execute(
            "SELECT series_id, observed_at, vintage_at, value, unit "
            "FROM observations ORDER BY series_id, observed_at, vintage_at"
        ):
            observations += 1
            series_id = str(row["series_id"])
            series = registry.get(series_id)
            identity = f"{series_id} {row['observed_at']} vintage {row['vintage_at']}"
            if series is None:
                record(f"{identity}: series is absent from the current registry")
                continue
            if str(row["unit"]) != series.unit:
                record(f"{identity}: unit {row['unit']!r}; expected {series.unit!r}")
                continue
            value = float(row["value"])
            if not math.isfinite(value):
                record(f"{identity}: value {value!r} is not finite")
                continue
            low = series.plausible_min
            high = series.plausible_max
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


def _validate_observation_read_capability(
    connection: sqlite3.Connection,
    schema_version: int,
) -> None:
    """Accept pre-migration stores only when the read-only scan contract exists."""

    required = {"series_id", "observed_at", "vintage_at", "value", "unit"}
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(observations)")}
    missing = sorted(required - columns)
    if missing:
        raise IndicatorsSchemaError(
            f"indicator SQLite schema {schema_version} cannot be validated; "
            f"observations is missing columns: {', '.join(missing)}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="validate every macro observation against the current registry"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = validate_store(args.db)
    except (IndicatorsSchemaError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"error: unable to validate indicator store {args.db}: {exc}", file=sys.stderr)
        return 1
    if report.valid:
        print(
            f"ok: schema v{report.schema_version}; {report.registry_series} registry series; "
            f"{report.observations} observations"
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


if __name__ == "__main__":
    raise SystemExit(main())
