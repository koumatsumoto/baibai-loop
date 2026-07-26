from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import cast

from .definitions import IndicatorDefinitions, SeriesDefinition, load_definitions

SQLITE_SCHEMA_VERSION = 6
# The acquisition outcome that says "this observation is withdrawn from the reads".
RETRACTED_STATUS = "retracted"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path("data/indicators/macro.sqlite")
_ROW_COUNT_SQL = {
    "series": "SELECT COUNT(*) FROM series",
    "aliases": "SELECT COUNT(*) FROM aliases",
    "observations": "SELECT COUNT(*) FROM observations",
    "provider_runs": "SELECT COUNT(*) FROM provider_runs",
}
_LEGACY_UNIT_RENAMES = {
    ("jp.foreign_flows", "jpy"): "jpy-thousand",
}
_LEGACY_UNIT_RENAME_MAX_SCHEMA_VERSION = 4
_SCHEMA_VALIDATION_SQL = {
    "main": {
        "user_version": "PRAGMA main.user_version",
        "registry_tables": (
            "SELECT name, sql FROM main.sqlite_master "
            "WHERE type = 'table' AND name IN "
            "('registry_state', 'registry_prune_authorizations')"
        ),
        "registry_state": (
            "SELECT singleton, generation, typeof(singleton), typeof(generation) "
            "FROM main.registry_state"
        ),
        "prune_authorizations": ("SELECT COUNT(*) FROM main.registry_prune_authorizations"),
        "triggers": "SELECT name, sql FROM main.sqlite_master WHERE type = 'trigger'",
    },
    "source": {
        "user_version": "PRAGMA source.user_version",
        "registry_tables": (
            "SELECT name, sql FROM source.sqlite_master "
            "WHERE type = 'table' AND name IN "
            "('registry_state', 'registry_prune_authorizations')"
        ),
        "registry_state": (
            "SELECT singleton, generation, typeof(singleton), typeof(generation) "
            "FROM source.registry_state"
        ),
        "prune_authorizations": ("SELECT COUNT(*) FROM source.registry_prune_authorizations"),
        "triggers": "SELECT name, sql FROM source.sqlite_master WHERE type = 'trigger'",
    },
}
_MIGRATE_V1_TO_V2_SQL = """
CREATE TABLE aliases_v2(
  alias TEXT NOT NULL,
  series_id TEXT NOT NULL REFERENCES series(series_id),
  PRIMARY KEY(alias, series_id)
);
INSERT OR IGNORE INTO aliases_v2(alias, series_id)
  SELECT alias, series_id FROM aliases;
DROP TABLE aliases;
ALTER TABLE aliases_v2 RENAME TO aliases;
CREATE INDEX IF NOT EXISTS idx_aliases_alias ON aliases(alias);
CREATE INDEX IF NOT EXISTS idx_observations_series_status_date_vintage
  ON observations(series_id, fetch_status, observed_at, vintage_at);
PRAGMA user_version = 2;
"""
_MIGRATE_V2_TO_V3_SQL = """
-- Version 3 is a semantic compatibility fence. Version 2 clients prune facts
-- during ordinary opens, so they must reject a store once non-destructive opens
-- become part of its contract.
CREATE TABLE IF NOT EXISTS registry_state(
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  generation INTEGER NOT NULL CHECK(generation >= 0)
);
INSERT OR IGNORE INTO registry_state(singleton, generation) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS registry_prune_authorizations(
  series_id TEXT PRIMARY KEY REFERENCES series(series_id) ON DELETE CASCADE
);
CREATE TRIGGER IF NOT EXISTS protect_series_from_implicit_prune
BEFORE DELETE ON series
WHEN NOT EXISTS(
  SELECT 1 FROM registry_prune_authorizations WHERE series_id = OLD.series_id
)
BEGIN
  SELECT RAISE(ABORT, 'explicit registry prune authorization required');
END;
PRAGMA user_version = 3;
"""
_MIGRATE_V3_TO_V4_SQL = """
ALTER TABLE series ADD COLUMN plausible_min REAL;
ALTER TABLE series ADD COLUMN plausible_max REAL;
CREATE TRIGGER validate_observation_plausibility_before_insert
BEFORE INSERT ON observations
WHEN NOT EXISTS (
  SELECT 1 FROM series
  WHERE series_id = NEW.series_id
    AND NEW.unit = unit
    AND (plausible_min IS NULL OR NEW.value >= plausible_min)
    AND (plausible_max IS NULL OR NEW.value <= plausible_max)
)
BEGIN
  SELECT RAISE(ABORT, 'observation violates series unit or plausible range');
END;
CREATE TRIGGER validate_observation_plausibility_before_update
BEFORE UPDATE OF series_id, value, unit ON observations
WHEN NOT EXISTS (
  SELECT 1 FROM series
  WHERE series_id = NEW.series_id
    AND NEW.unit = unit
    AND (plausible_min IS NULL OR NEW.value >= plausible_min)
    AND (plausible_max IS NULL OR NEW.value <= plausible_max)
)
BEGIN
  SELECT RAISE(ABORT, 'observation violates series unit or plausible range');
END;
CREATE TRIGGER validate_series_contract_before_update
BEFORE UPDATE OF unit, plausible_min, plausible_max ON series
WHEN EXISTS (
  SELECT 1 FROM observations
  WHERE series_id = NEW.series_id
    AND (
      unit != NEW.unit
      OR (NEW.plausible_min IS NOT NULL AND value < NEW.plausible_min)
      OR (NEW.plausible_max IS NOT NULL AND value > NEW.plausible_max)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'series contract excludes an existing observation');
END;
PRAGMA user_version = 4;
"""
_MIGRATE_V4_TO_V5_SQL = """
DROP TRIGGER validate_observation_plausibility_before_insert;
DROP TRIGGER validate_observation_plausibility_before_update;
DROP TRIGGER validate_series_contract_before_update;
UPDATE observations
SET unit = 'jpy-thousand'
WHERE series_id = 'jp.foreign_flows' AND unit = 'jpy';
UPDATE series
SET unit = 'jpy-thousand'
WHERE series_id = 'jp.foreign_flows' AND unit = 'jpy';
CREATE TRIGGER validate_observation_plausibility_before_insert
BEFORE INSERT ON observations
WHEN NOT EXISTS (
  SELECT 1 FROM series
  WHERE series_id = NEW.series_id
    AND NEW.unit = unit
    AND (plausible_min IS NULL OR NEW.value >= plausible_min)
    AND (plausible_max IS NULL OR NEW.value <= plausible_max)
)
BEGIN
  SELECT RAISE(ABORT, 'observation violates series unit or plausible range');
END;
CREATE TRIGGER validate_observation_plausibility_before_update
BEFORE UPDATE OF series_id, value, unit ON observations
WHEN NOT EXISTS (
  SELECT 1 FROM series
  WHERE series_id = NEW.series_id
    AND NEW.unit = unit
    AND (plausible_min IS NULL OR NEW.value >= plausible_min)
    AND (plausible_max IS NULL OR NEW.value <= plausible_max)
)
BEGIN
  SELECT RAISE(ABORT, 'observation violates series unit or plausible range');
END;
CREATE TRIGGER validate_series_contract_before_update
BEFORE UPDATE OF unit, plausible_min, plausible_max ON series
WHEN EXISTS (
  SELECT 1 FROM observations
  WHERE series_id = NEW.series_id
    AND (
      unit != NEW.unit
      OR (NEW.plausible_min IS NOT NULL AND value < NEW.plausible_min)
      OR (NEW.plausible_max IS NOT NULL AND value > NEW.plausible_max)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'series contract excludes an existing observation');
END;
PRAGMA user_version = 5;
"""
_MIGRATE_V5_TO_V6_SQL = """
-- Version 6 widens the fetch_status domain with 'retracted'. A CHECK constraint can
-- only change by rebuilding the table, and every trigger that names `observations`
-- has to stand aside while the table is swapped, so all three are dropped and
-- recreated verbatim around the copy.
DROP TRIGGER validate_observation_plausibility_before_insert;
DROP TRIGGER validate_observation_plausibility_before_update;
DROP TRIGGER validate_series_contract_before_update;
CREATE TABLE observations_v6(
  series_id TEXT NOT NULL REFERENCES series(series_id),
  observed_at TEXT NOT NULL,
  period_start TEXT,
  period_end TEXT,
  value REAL NOT NULL,
  unit TEXT NOT NULL,
  vintage_at TEXT NOT NULL,
  fetch_status TEXT NOT NULL,
  source_url TEXT NOT NULL,
  PRIMARY KEY(series_id, observed_at, vintage_at),
  CHECK(fetch_status IN ('ok', 'failed', 'unreleased', 'retracted'))
);
INSERT INTO observations_v6(
  series_id, observed_at, period_start, period_end, value, unit,
  vintage_at, fetch_status, source_url
)
SELECT series_id, observed_at, period_start, period_end, value, unit,
       vintage_at, fetch_status, source_url
FROM observations;
DROP TABLE observations;
ALTER TABLE observations_v6 RENAME TO observations;
CREATE INDEX IF NOT EXISTS idx_observations_series_date
  ON observations(series_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_observations_series_status_date_vintage
  ON observations(series_id, fetch_status, observed_at, vintage_at);
CREATE TRIGGER validate_observation_plausibility_before_insert
BEFORE INSERT ON observations
WHEN NOT EXISTS (
  SELECT 1 FROM series
  WHERE series_id = NEW.series_id
    AND NEW.unit = unit
    AND (plausible_min IS NULL OR NEW.value >= plausible_min)
    AND (plausible_max IS NULL OR NEW.value <= plausible_max)
)
BEGIN
  SELECT RAISE(ABORT, 'observation violates series unit or plausible range');
END;
CREATE TRIGGER validate_observation_plausibility_before_update
BEFORE UPDATE OF series_id, value, unit ON observations
WHEN NOT EXISTS (
  SELECT 1 FROM series
  WHERE series_id = NEW.series_id
    AND NEW.unit = unit
    AND (plausible_min IS NULL OR NEW.value >= plausible_min)
    AND (plausible_max IS NULL OR NEW.value <= plausible_max)
)
BEGIN
  SELECT RAISE(ABORT, 'observation violates series unit or plausible range');
END;
CREATE TRIGGER validate_series_contract_before_update
BEFORE UPDATE OF unit, plausible_min, plausible_max ON series
WHEN EXISTS (
  SELECT 1 FROM observations
  WHERE series_id = NEW.series_id
    AND (
      unit != NEW.unit
      OR (NEW.plausible_min IS NOT NULL AND value < NEW.plausible_min)
      OR (NEW.plausible_max IS NOT NULL AND value > NEW.plausible_max)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'series contract excludes an existing observation');
END;
PRAGMA user_version = 6;
"""


class IndicatorsSchemaError(RuntimeError):
    """Raised when the indicator SQLite schema is missing or unsupported."""


def normalize_observation_unit(
    series_id: str,
    unit: str,
    *,
    schema_version: int,
) -> str:
    """Map a released legacy unit name to the current registry contract."""

    if legacy_unit_renames_apply(schema_version):
        return _LEGACY_UNIT_RENAMES.get((series_id, unit), unit)
    return unit


def legacy_unit_renames_apply(schema_version: int) -> bool:
    """Limit compatibility to schemas that actually wrote legacy unit names."""

    return 1 <= schema_version <= _LEGACY_UNIT_RENAME_MAX_SCHEMA_VERSION


@dataclass(frozen=True)
class ObservationRecord:
    series_id: str
    observed_at: date
    value: float
    unit: str
    source_url: str
    period_start: date | None = None
    period_end: date | None = None
    vintage_at: datetime | None = None
    fetch_status: str = "ok"


@dataclass(frozen=True, slots=True)
class RetractionOutcome:
    """What a retraction withdrew, and what the reads fell back to."""

    series_id: str
    observed_at: date
    withdrawn: ObservationRecord
    # The observation that stood under the withdrawn vintage and reads again, or None
    # when nothing did and the date left the reads.
    restored: ObservationRecord | None


@dataclass(frozen=True, slots=True)
class RegistryPruneResult:
    series_id: str
    observation_rows: int
    provider_run_rows: int


def initialize_database(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    definitions: IndicatorDefinitions | None = None,
) -> sqlite3.Connection:
    conn = _connect(db_path)
    try:
        resolved_definitions = definitions or load_definitions()
        _ensure_schema(conn, resolved_definitions)
        seed_definitions(conn, resolved_definitions)
        set_registry_generation(conn, resolved_definitions.generation)
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def open_connection(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    definitions: IndicatorDefinitions | None = None,
) -> sqlite3.Connection:
    if not db_path.exists():
        return initialize_database(db_path, definitions=definitions)
    conn = _connect(db_path)
    try:
        resolved_definitions = definitions or load_definitions()
        _ensure_schema(conn, resolved_definitions)
        seed_definitions(conn, resolved_definitions)
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def validate_current_schema(
    conn: sqlite3.Connection,
    *,
    schema: str = "main",
) -> None:
    validate_schema_contract(
        conn,
        schema=schema,
        expected_version=SQLITE_SCHEMA_VERSION,
    )


def validate_schema_contract(
    conn: sqlite3.Connection,
    *,
    schema: str,
    expected_version: int,
) -> None:
    if schema not in {"main", "source"}:
        raise ValueError(f"unsupported SQLite schema name: {schema!r}")
    queries = _SCHEMA_VALIDATION_SQL[schema]
    version = int(conn.execute(queries["user_version"]).fetchone()[0])
    if version != expected_version:
        raise IndicatorsSchemaError(
            f"unsupported indicator SQLite schema: {version}; expected {expected_version}"
        )
    _validate_registry_state_contract(conn, schema=schema)
    _validate_trigger_contract(conn, schema=schema)


def _validate_registry_state_contract(conn: sqlite3.Connection, *, schema: str) -> None:
    queries = _SCHEMA_VALIDATION_SQL[schema]
    expected = _canonical_registry_table_sql()
    actual = {
        str(row[0]): _normalize_schema_sql(str(row[1]))
        for row in conn.execute(queries["registry_tables"])
    }
    missing = sorted(set(expected) - set(actual))
    changed = sorted(name for name in set(expected) & set(actual) if actual[name] != expected[name])
    if missing or changed:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if changed:
            details.append(f"changed: {', '.join(changed)}")
        raise IndicatorsSchemaError(
            f"indicator SQLite registry table contract mismatch in {schema} ({'; '.join(details)})"
        )

    rows = conn.execute(queries["registry_state"]).fetchall()
    if (
        len(rows) != 1
        or rows[0][0] != 1
        or rows[0][2] != "integer"
        or rows[0][3] != "integer"
        or int(rows[0][1]) < 0
    ):
        raise IndicatorsSchemaError(
            f"indicator SQLite registry state in {schema} must contain exactly "
            "singleton=1 with a non-negative integer generation"
        )
    authorizations = int(conn.execute(queries["prune_authorizations"]).fetchone()[0])
    if authorizations:
        raise IndicatorsSchemaError(
            f"indicator SQLite registry prune authorization state in {schema} "
            f"must be empty; found {authorizations}"
        )


def _validate_trigger_contract(conn: sqlite3.Connection, *, schema: str) -> None:
    queries = _SCHEMA_VALIDATION_SQL[schema]
    actual = {
        str(row[0]): _normalize_schema_sql(str(row[1])) for row in conn.execute(queries["triggers"])
    }
    expected = _canonical_trigger_sql()
    missing = sorted(set(expected) - set(actual))
    changed = sorted(name for name in set(expected) & set(actual) if actual[name] != expected[name])
    unexpected = sorted(set(actual) - set(expected))
    if missing or changed or unexpected:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if changed:
            details.append(f"changed: {', '.join(changed)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        raise IndicatorsSchemaError(
            f"indicator SQLite trigger contract mismatch in {schema} ({'; '.join(details)})"
        )


@cache
def _canonical_trigger_sql() -> dict[str, str]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        return {
            str(row[0]): _normalize_schema_sql(str(row[1]))
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    finally:
        connection.close()


@cache
def _canonical_registry_table_sql() -> dict[str, str]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        return {
            str(row[0]): _normalize_schema_sql(str(row[1]))
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'table' AND name IN "
                "('registry_state', 'registry_prune_authorizations')"
            )
        }
    finally:
        connection.close()


def _normalize_schema_sql(sql: str) -> str:
    return " ".join(sql.casefold().split()).replace(
        " if not exists",
        "",
        1,
    )


def seed_definitions(conn: sqlite3.Connection, definitions: IndicatorDefinitions) -> None:
    _validate_existing_observations(conn, definitions)
    for series in definitions.series:
        conn.execute(
            "INSERT INTO series("
            "series_id, name, category, geography, frequency, unit, provider, provider_series_id, "
            "source_id, source_url, priority, notes, plausible_min, plausible_max"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(series_id) DO UPDATE SET "
            "name = excluded.name, category = excluded.category, geography = excluded.geography, "
            "frequency = excluded.frequency, unit = excluded.unit, provider = excluded.provider, "
            "provider_series_id = excluded.provider_series_id, source_id = excluded.source_id, "
            "source_url = excluded.source_url, priority = excluded.priority, "
            "notes = excluded.notes, plausible_min = excluded.plausible_min, "
            "plausible_max = excluded.plausible_max",
            (
                series.series_id,
                series.name,
                series.category,
                series.geography,
                series.frequency,
                series.unit,
                series.provider,
                series.provider_series_id,
                series.source_id,
                series.source_url,
                series.priority,
                series.notes,
                series.plausible_min,
                series.plausible_max,
            ),
        )
        # Replace aliases only for definitions this registry knows. A stale
        # branch must not erase aliases of newer series whose facts are retained.
        conn.execute("DELETE FROM aliases WHERE series_id = ?", (series.series_id,))
        conn.execute(
            "INSERT OR REPLACE INTO aliases(alias, series_id) VALUES (?, ?)",
            (series.name, series.series_id),
        )
        for alias in series.aliases:
            conn.execute(
                "INSERT OR REPLACE INTO aliases(alias, series_id) VALUES (?, ?)",
                (alias, series.series_id),
            )


def _validate_existing_observations(
    conn: sqlite3.Connection,
    definitions: IndicatorDefinitions,
    *,
    schema_version: int = SQLITE_SCHEMA_VERSION,
) -> None:
    """Fail before a registry update could contradict facts already in the store."""

    for series in definitions.series:
        row = conn.execute(
            """
            SELECT observed_at, vintage_at, value, unit
            FROM observations
            WHERE series_id = ?
              AND (
                CASE
                  WHEN ? BETWEEN 1 AND 4
                   AND series_id = 'jp.foreign_flows'
                   AND unit = 'jpy'
                  THEN 'jpy-thousand'
                  ELSE unit
                END != ?
                OR (? IS NOT NULL AND value < ?)
                OR (? IS NOT NULL AND value > ?)
              )
            ORDER BY observed_at, vintage_at
            LIMIT 1
            """,
            (
                series.series_id,
                schema_version,
                series.unit,
                series.plausible_min,
                series.plausible_min,
                series.plausible_max,
                series.plausible_max,
            ),
        ).fetchone()
        if row is None:
            continue
        normalized_unit = normalize_observation_unit(
            series.series_id,
            str(row["unit"]),
            schema_version=schema_version,
        )
        if normalized_unit != series.unit:
            detail = f"unit {row['unit']!r}; expected {series.unit!r}"
        else:
            low = "-inf" if series.plausible_min is None else f"{series.plausible_min:g}"
            high = "inf" if series.plausible_max is None else f"{series.plausible_max:g}"
            detail = f"value {float(row['value']):g} outside plausible range [{low}, {high}]"
        raise IndicatorsSchemaError(
            f"stored observation violates registry contract: {series.series_id} "
            f"{row['observed_at']} vintage {row['vintage_at']}: {detail}"
        )


def _validate_foreign_key_integrity(conn: sqlite3.Connection) -> None:
    """Reject legacy stores whose disabled-FK writes left orphaned facts."""

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        table, rowid, parent, constraint = violations[0]
        raise IndicatorsSchemaError(
            "indicator SQLite foreign key contract is invalid: "
            f"{table} rowid {rowid} references {parent} "
            f"(constraint {constraint}); {len(violations)} violation(s)"
        )


def unregistered_series_ids(
    conn: sqlite3.Connection,
    definitions: IndicatorDefinitions,
) -> tuple[str, ...]:
    """Series the store carries that the given registry snapshot does not name."""

    registered = {series.series_id for series in definitions.series}
    stored = {
        str(row["series_id"]) for row in conn.execute("SELECT series_id FROM series").fetchall()
    }
    return tuple(sorted(stored - registered))


def prune_definitions(
    conn: sqlite3.Connection,
    definitions: IndicatorDefinitions,
) -> tuple[RegistryPruneResult, ...]:
    """Delete facts for series absent from an explicitly trusted registry snapshot."""

    results: list[RegistryPruneResult] = []
    for series_id in unregistered_series_ids(conn, definitions):
        observation_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM observations WHERE series_id = ?",
                (series_id,),
            ).fetchone()[0]
        )
        provider_run_rows = int(
            conn.execute(
                "SELECT COUNT(*) FROM provider_runs WHERE series_id = ?",
                (series_id,),
            ).fetchone()[0]
        )
        conn.execute(
            "INSERT INTO registry_prune_authorizations(series_id) VALUES (?)",
            (series_id,),
        )
        conn.execute("DELETE FROM provider_runs WHERE series_id = ?", (series_id,))
        conn.execute("DELETE FROM observations WHERE series_id = ?", (series_id,))
        conn.execute("DELETE FROM aliases WHERE series_id = ?", (series_id,))
        conn.execute("DELETE FROM series WHERE series_id = ?", (series_id,))
        results.append(
            RegistryPruneResult(
                series_id=series_id,
                observation_rows=observation_rows,
                provider_run_rows=provider_run_rows,
            )
        )
    set_registry_generation(conn, definitions.generation)
    return tuple(results)


def registry_generation(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT generation FROM registry_state WHERE singleton = 1").fetchone()
    if row is None:
        raise IndicatorsSchemaError("indicator registry state is missing")
    return int(row[0])


def set_registry_generation(conn: sqlite3.Connection, generation: int) -> None:
    cursor = conn.execute(
        "UPDATE registry_state SET generation = ? WHERE singleton = 1",
        (generation,),
    )
    if cursor.rowcount != 1:
        raise IndicatorsSchemaError("indicator registry state is missing")


def get_series(conn: sqlite3.Connection, series_id: str) -> SeriesDefinition:
    row = conn.execute("SELECT * FROM series WHERE series_id = ?", (series_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown indicator series: {series_id}")
    alias_rows = conn.execute(
        "SELECT alias FROM aliases WHERE series_id = ? ORDER BY alias",
        (series_id,),
    ).fetchall()
    return _series_from_row(row, aliases=tuple(str(item["alias"]) for item in alias_rows))


def open_read_only_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the store for reading only, so a read command cannot alter it.

    ``open_connection`` refreshes registry metadata and aliases, while this path
    also prevents those non-destructive writes for immutable consumers.
    """

    resolved = db_path.resolve()
    if not resolved.exists():
        raise IndicatorsSchemaError(f"indicators store not found: {resolved}")
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def list_series(
    conn: sqlite3.Connection, *, category: str | None = None
) -> tuple[SeriesDefinition, ...]:
    params: tuple[str, ...] = ()
    sql = "SELECT * FROM series"
    if category is not None:
        sql += " WHERE category = ?"
        params = (category,)
    sql += " ORDER BY priority, series_id"
    return tuple(_series_from_row(row, aliases=()) for row in conn.execute(sql, params).fetchall())


def search_series(conn: sqlite3.Connection, query: str) -> tuple[SeriesDefinition, ...]:
    needle = f"%{query.casefold()}%"
    rows = conn.execute(
        "SELECT DISTINCT s.* FROM series s "
        "LEFT JOIN aliases a ON a.series_id = s.series_id "
        "WHERE lower(s.series_id) LIKE ? OR lower(s.name) LIKE ? "
        "OR lower(s.category) LIKE ? OR lower(s.geography) LIKE ? OR lower(a.alias) LIKE ? "
        "ORDER BY s.priority, s.series_id",
        (needle, needle, needle, needle, needle),
    ).fetchall()
    return tuple(_series_from_row(row, aliases=()) for row in rows)


def insert_observations(
    conn: sqlite3.Connection,
    observations: list[ObservationRecord],
    *,
    deduplicate_unchanged: bool = True,
) -> None:
    savepoint = f"insert_observations_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        rows = _observation_rows(observations)
        if deduplicate_unchanged:
            rows = _observation_rows_without_unchanged_vintages(conn, rows)
        conn.executemany(
            "INSERT INTO observations("
            "series_id, observed_at, period_start, period_end, value, unit, vintage_at, "
            "fetch_status, source_url"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(series_id, observed_at, vintage_at) DO UPDATE SET "
            "period_start = excluded.period_start, period_end = excluded.period_end, "
            "value = excluded.value, unit = excluded.unit, fetch_status = excluded.fetch_status, "
            "source_url = excluded.source_url",
            rows,
        )
    except BaseException:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE {savepoint}")


def _observation_rows(observations: list[ObservationRecord]) -> list[tuple[object, ...]]:
    if not observations:
        return []
    default_vintage = datetime.now(UTC)
    return [
        (
            item.series_id,
            item.observed_at.isoformat(),
            item.period_start.isoformat() if item.period_start else None,
            item.period_end.isoformat() if item.period_end else None,
            item.value,
            item.unit,
            (item.vintage_at or default_vintage).isoformat(),
            item.fetch_status,
            item.source_url,
        )
        for item in observations
    ]


def _observation_rows_without_unchanged_vintages(
    conn: sqlite3.Connection,
    rows: list[tuple[object, ...]],
) -> list[tuple[object, ...]]:
    latest: dict[tuple[str, str], tuple[object, ...]] = {}
    by_series: dict[str, list[str]] = {}
    for series_id, observed_at, *_ in rows:
        by_series.setdefault(cast(str, series_id), []).append(cast(str, observed_at))
    for series_id, observed_dates in by_series.items():
        for row in conn.execute(
            "WITH ranked AS ("
            "SELECT series_id, observed_at, period_start, period_end, value, unit, "
            "vintage_at, fetch_status, source_url, "
            "ROW_NUMBER() OVER (PARTITION BY series_id, observed_at "
            "ORDER BY vintage_at DESC) AS rank "
            "FROM observations WHERE series_id = ? AND observed_at BETWEEN ? AND ?"
            ") SELECT series_id, observed_at, period_start, period_end, value, unit, "
            "vintage_at, fetch_status, source_url FROM ranked WHERE rank = 1",
            (series_id, min(observed_dates), max(observed_dates)),
        ):
            latest[(str(row["series_id"]), str(row["observed_at"]))] = tuple(row)

    kept: list[tuple[object, ...]] = []
    for row in sorted(rows, key=lambda item: (str(item[0]), str(item[1]), str(item[6]))):
        key = (cast(str, row[0]), cast(str, row[1]))
        current = latest.get(key)
        if (
            current is not None
            and str(row[6]) >= str(current[6])
            and _same_observation(row, current)
        ):
            latest[key] = row
            continue
        kept.append(row)
        if current is None or str(row[6]) >= str(current[6]):
            latest[key] = row
    return kept


def _same_observation(left: tuple[object, ...], right: tuple[object, ...]) -> bool:
    content_indexes = (2, 3, 4, 5, 7, 8)
    return all(left[index] == right[index] for index in content_indexes)


def delete_unchanged_vintages(conn: sqlite3.Connection, series_id: str) -> int:
    changes_before = conn.total_changes
    conn.execute(
        "WITH ordered AS ("
        "SELECT rowid AS row_id, "
        "ROW_NUMBER() OVER (PARTITION BY series_id, observed_at ORDER BY vintage_at) AS position, "
        "period_start, period_end, value, unit, fetch_status, source_url, "
        "LAG(period_start) OVER window AS previous_period_start, "
        "LAG(period_end) OVER window AS previous_period_end, "
        "LAG(value) OVER window AS previous_value, "
        "LAG(unit) OVER window AS previous_unit, "
        "LAG(fetch_status) OVER window AS previous_fetch_status, "
        "LAG(source_url) OVER window AS previous_source_url "
        "FROM observations WHERE series_id = ? "
        "WINDOW window AS (PARTITION BY series_id, observed_at ORDER BY vintage_at)"
        ") DELETE FROM observations WHERE rowid IN ("
        "SELECT row_id FROM ordered WHERE position > 1 "
        "AND period_start IS previous_period_start "
        "AND period_end IS previous_period_end "
        "AND value IS previous_value "
        "AND unit IS previous_unit "
        "AND fetch_status IS previous_fetch_status "
        "AND source_url IS previous_source_url"
        ")",
        (series_id,),
    )
    return conn.total_changes - changes_before


def record_provider_run(
    conn: sqlite3.Connection,
    *,
    provider: str,
    series_id: str,
    start: date,
    end: date,
    started_at: datetime,
    status: str,
    record_count: int,
    error_message: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO provider_runs("
        "run_id, provider, series_id, range_start, range_end, started_at, finished_at, "
        "status, record_count, error_message"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            f"run-{uuid.uuid4().hex}",
            provider,
            series_id,
            start.isoformat(),
            end.isoformat(),
            started_at.isoformat(),
            datetime.now(UTC).isoformat(),
            status,
            record_count,
            error_message,
        ),
    )


def has_ok_coverage(conn: sqlite3.Connection, series_id: str, start: date, end: date) -> bool:
    row = conn.execute(
        "SELECT 1 FROM provider_runs "
        "WHERE series_id = ? AND status = 'ok' "
        "AND record_count > 0 "
        "AND range_start <= ? AND range_end >= ? "
        "ORDER BY finished_at DESC LIMIT 1",
        (series_id, start.isoformat(), end.isoformat()),
    ).fetchone()
    return row is not None


def observations_in_range(
    conn: sqlite3.Connection,
    series_id: str,
    start: date,
    end: date,
    *,
    point_in_time: bool = False,
    vintage_on_or_before: date | None = None,
) -> tuple[ObservationRecord, ...]:
    # ``point_in_time`` clamps to observations published by the explicit vintage
    # cutoff, or by ``end`` when the caller uses one date for both dimensions.
    # Non-point-in-time providers still return the latest acquisition vintage.
    #
    # The newest vintage is chosen among 'ok' and 'retracted' rows, then only 'ok' is
    # returned: a retraction is the newest thing known about that observation date, so
    # it hides it. 'failed' and 'unreleased' are acquisition outcomes rather than
    # statements about the value, so they never hide an observation that was read.
    end_text = end.isoformat()
    vintage_text = (vintage_on_or_before or end).isoformat()
    pit = 1 if point_in_time else 0
    rows = conn.execute(
        "SELECT o.* FROM observations o "
        "WHERE o.series_id = ? "
        "AND o.observed_at BETWEEN ? AND ? "
        "AND o.fetch_status = 'ok' "
        "AND (NOT ? OR substr(o.vintage_at, 1, 10) <= ?) "
        "AND o.vintage_at = ("
        "SELECT MAX(inner_o.vintage_at) FROM observations inner_o "
        "WHERE inner_o.series_id = o.series_id "
        "AND inner_o.observed_at = o.observed_at "
        "AND inner_o.fetch_status IN ('ok', 'retracted') "
        "AND (NOT ? OR substr(inner_o.vintage_at, 1, 10) <= ?)"
        ") ORDER BY o.observed_at",
        (
            series_id,
            start.isoformat(),
            end_text,
            pit,
            vintage_text,
            pit,
            vintage_text,
        ),
    ).fetchall()
    return tuple(_observation_from_row(row) for row in rows)


def latest_observation(
    conn: sqlite3.Connection,
    series_id: str,
    *,
    on_or_before: date | None = None,
    on_or_after: date | None = None,
    point_in_time: bool = False,
) -> ObservationRecord | None:
    cutoff = on_or_before.isoformat() if on_or_before is not None else None
    floor = on_or_after.isoformat() if on_or_after is not None else None
    pit = 1 if point_in_time else 0
    row = conn.execute(
        "SELECT o.* FROM observations o "
        "WHERE o.series_id = ? AND o.fetch_status = 'ok' "
        "AND (? IS NULL OR o.observed_at <= ?) "
        "AND (? IS NULL OR o.observed_at >= ?) "
        "AND (NOT ? OR ? IS NULL "
        "OR substr(o.vintage_at, 1, 10) <= ?) "
        # A retracted date is not the latest observation, so the newest vintage is
        # resolved the same way the range read resolves it.
        "AND o.vintage_at = ("
        "SELECT MAX(inner_o.vintage_at) FROM observations inner_o "
        "WHERE inner_o.series_id = o.series_id "
        "AND inner_o.observed_at = o.observed_at "
        "AND inner_o.fetch_status IN ('ok', 'retracted') "
        "AND (NOT ? OR ? IS NULL OR substr(inner_o.vintage_at, 1, 10) <= ?)"
        ") "
        "ORDER BY o.observed_at DESC LIMIT 1",
        (
            series_id,
            cutoff,
            cutoff,
            floor,
            floor,
            pit,
            cutoff,
            cutoff,
            pit,
            cutoff,
            cutoff,
        ),
    ).fetchone()
    return _observation_from_row(row) if row is not None else None


def retract_observations(
    conn: sqlite3.Connection,
    series_id: str,
    observed_dates: Sequence[date],
    *,
    vintage_at: datetime,
) -> tuple[RetractionOutcome, ...]:
    """Withdraw the latest vintage of an observation date and let the belief under it stand.

    Deleting the row does not hold. The merge that keeps this store and the cloud copy
    convergent restores every fact either side has, so a delete comes back on the next
    push — the property that protects real history, and the reason a wrong row needs a
    different exit. The exit is another vintage: what the store now knows about that
    date is written on top, so it travels through the merge like any other row, and a
    point-in-time replay whose cutoff predates it still sees what was believed then.

    What gets written on top is whatever stood *below* the withdrawn vintage. A date
    whose earlier vintage is a good observation goes back to reading that observation —
    the common case when a faulty writer stamped a wrong value over a correct one. A
    date with nothing under it, or with a retraction under it, is withdrawn from the
    reads entirely.

    Either way the written row carries a stored value and unit, so the plausibility
    contract still holds for it. A date whose latest vintage is already retracted is
    left alone; a date with no observation at all is an error, because retracting
    something that was never stored means the caller is working from a stale list.
    """

    outcomes: list[RetractionOutcome] = []
    written: list[ObservationRecord] = []
    for observed_at in observed_dates:
        rows = conn.execute(
            "SELECT * FROM observations WHERE series_id = ? AND observed_at = ? "
            "AND fetch_status IN ('ok', ?) ORDER BY vintage_at DESC LIMIT 2",
            (series_id, observed_at.isoformat(), RETRACTED_STATUS),
        ).fetchall()
        if not rows:
            raise ValueError(f"no observation to retract: {series_id} {observed_at.isoformat()}")
        withdrawn = _observation_from_row(rows[0])
        if withdrawn.fetch_status == RETRACTED_STATUS:
            continue
        underneath = _observation_from_row(rows[1]) if len(rows) > 1 else None
        if underneath is not None and underneath.fetch_status == RETRACTED_STATUS:
            underneath = None
        if underneath is None:
            written.append(replace(withdrawn, vintage_at=vintage_at, fetch_status=RETRACTED_STATUS))
        else:
            written.append(replace(underneath, vintage_at=vintage_at))
        outcomes.append(
            RetractionOutcome(
                series_id=series_id,
                observed_at=observed_at,
                withdrawn=withdrawn,
                restored=underneath,
            )
        )
    if written:
        insert_observations(conn, written, deduplicate_unchanged=False)
    return tuple(outcomes)


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_schema(
    conn: sqlite3.Connection,
    definitions: IndicatorDefinitions,
) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version == SQLITE_SCHEMA_VERSION:
        validate_current_schema(conn)
        return
    if version not in {0, 1, 2, 3, 4, 5}:
        raise IndicatorsSchemaError(
            f"unsupported indicator SQLite schema: {version}; expected {SQLITE_SCHEMA_VERSION}"
        )
    if version == 0:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        validate_current_schema(conn)
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        if version in {4, 5}:
            validate_schema_contract(
                conn,
                schema="main",
                expected_version=version,
            )
        _validate_foreign_key_integrity(conn)
        _validate_existing_observations(
            conn,
            definitions,
            schema_version=version,
        )
        if version == 1:
            _execute_sql_statements(conn, _MIGRATE_V1_TO_V2_SQL)
            version = 2
        if version == 2:
            _execute_sql_statements(conn, _MIGRATE_V2_TO_V3_SQL)
            version = 3
        if version == 3:
            _execute_sql_statements(conn, _MIGRATE_V3_TO_V4_SQL)
            version = 4
        if version == 4:
            _execute_sql_statements(conn, _MIGRATE_V4_TO_V5_SQL)
            version = 5
        if version == 5:
            _execute_sql_statements(conn, _MIGRATE_V5_TO_V6_SQL)
        validate_current_schema(conn)
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def _execute_sql_statements(conn: sqlite3.Connection, script: str) -> None:
    """Execute migration DDL without sqlite3.executescript's implicit commit."""

    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if not sqlite3.complete_statement(statement):
            continue
        conn.execute(statement)
        statement = ""
    if statement.strip():
        raise IndicatorsSchemaError("incomplete indicator schema migration statement")


def _series_from_row(row: sqlite3.Row, *, aliases: tuple[str, ...]) -> SeriesDefinition:
    return SeriesDefinition(
        series_id=str(row["series_id"]),
        name=str(row["name"]),
        category=str(row["category"]),
        geography=str(row["geography"]),
        frequency=str(row["frequency"]),
        unit=str(row["unit"]),
        provider=str(row["provider"]),
        provider_series_id=str(row["provider_series_id"]),
        source_id=str(row["source_id"]),
        source_url=str(row["source_url"]),
        priority=int(row["priority"]),
        notes=str(row["notes"]) if row["notes"] is not None else None,
        plausible_min=(float(row["plausible_min"]) if row["plausible_min"] is not None else None),
        plausible_max=(float(row["plausible_max"]) if row["plausible_max"] is not None else None),
        aliases=aliases,
    )


def _observation_from_row(row: sqlite3.Row) -> ObservationRecord:
    period_start = row["period_start"]
    period_end = row["period_end"]
    vintage_at = row["vintage_at"]
    return ObservationRecord(
        series_id=str(row["series_id"]),
        observed_at=date.fromisoformat(str(row["observed_at"])),
        period_start=date.fromisoformat(str(period_start)) if period_start else None,
        period_end=date.fromisoformat(str(period_end)) if period_end else None,
        value=float(row["value"]),
        unit=str(row["unit"]),
        vintage_at=datetime.fromisoformat(str(vintage_at)) if vintage_at else None,
        fetch_status=str(row["fetch_status"]),
        source_url=str(row["source_url"]),
    )


def row_count(conn: sqlite3.Connection, table: str) -> int:
    sql = _ROW_COUNT_SQL.get(table)
    if sql is None:
        raise ValueError(f"unsupported indicator table: {table}")
    return cast(int, conn.execute(sql).fetchone()[0])
