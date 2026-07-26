from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path
from typing import cast

from .definitions import IndicatorDefinitions, SeriesDefinition, load_definitions

SQLITE_SCHEMA_VERSION = 4
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path("data/indicators/macro.sqlite")
_ROW_COUNT_SQL = {
    "series": "SELECT COUNT(*) FROM series",
    "aliases": "SELECT COUNT(*) FROM aliases",
    "registry_series": "SELECT COUNT(*) FROM registry_series",
    "observations": "SELECT COUNT(*) FROM observations",
    "provider_runs": "SELECT COUNT(*) FROM provider_runs",
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
-- Version 3 fences clients whose registry seed deleted absent series on every
-- writable open. Membership records the last registry snapshot applied by a
-- successful refresh; ordinary opens never change it.
CREATE TABLE IF NOT EXISTS registry_series(
  series_id TEXT PRIMARY KEY REFERENCES series(series_id)
);
INSERT OR IGNORE INTO registry_series(series_id) SELECT series_id FROM series;
PRAGMA user_version = 3;
"""
_MIGRATE_V3_TO_V4_SQL = """
BEGIN IMMEDIATE;
ALTER TABLE series ADD COLUMN plausible_min REAL;
ALTER TABLE series ADD COLUMN plausible_max REAL;
CREATE TRIGGER validate_observation_plausibility_before_insert
BEFORE INSERT ON observations
WHEN EXISTS (
  SELECT 1 FROM series
  WHERE series_id = NEW.series_id
    AND (
      NEW.unit != unit
      OR (plausible_min IS NOT NULL AND NEW.value < plausible_min)
      OR (plausible_max IS NOT NULL AND NEW.value > plausible_max)
    )
)
BEGIN
  SELECT RAISE(ABORT, 'observation violates series unit or plausible range');
END;
CREATE TRIGGER validate_observation_plausibility_before_update
BEFORE UPDATE OF series_id, value, unit ON observations
WHEN EXISTS (
  SELECT 1 FROM series
  WHERE series_id = NEW.series_id
    AND (
      NEW.unit != unit
      OR (plausible_min IS NOT NULL AND NEW.value < plausible_min)
      OR (plausible_max IS NOT NULL AND NEW.value > plausible_max)
    )
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
COMMIT;
"""


class IndicatorsSchemaError(RuntimeError):
    """Raised when the indicator SQLite schema is missing or unsupported."""


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
class RegistryPruneResult:
    series_id: str
    observation_rows: int
    provider_run_rows: int


def initialize_database(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    definitions: IndicatorDefinitions | None = None,
) -> sqlite3.Connection:
    resolved_definitions = definitions or load_definitions()
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        seed_definitions(conn, resolved_definitions)
        apply_registry_membership(conn, resolved_definitions)
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
        _ensure_schema(conn)
        seed_definitions(conn, definitions or load_definitions())
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
    if schema not in {"main", "source"}:
        raise ValueError(f"unsupported SQLite schema name: {schema!r}")
    version = int(conn.execute(f"PRAGMA {schema}.user_version").fetchone()[0])
    if version != SQLITE_SCHEMA_VERSION:
        raise IndicatorsSchemaError(
            f"unsupported indicator SQLite schema: {version}; expected {SQLITE_SCHEMA_VERSION}"
        )
    _validate_trigger_contract(conn, schema=schema)


def _validate_trigger_contract(conn: sqlite3.Connection, *, schema: str) -> None:
    actual = {
        str(row[0]): _normalize_trigger_sql(str(row[1]))
        for row in conn.execute(
            f"SELECT name, sql FROM {schema}.sqlite_master WHERE type = 'trigger'"
        )
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
            str(row[0]): _normalize_trigger_sql(str(row[1]))
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    finally:
        connection.close()


def _normalize_trigger_sql(sql: str) -> str:
    return " ".join(sql.casefold().split()).replace(
        "create trigger if not exists",
        "create trigger",
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
) -> None:
    """Fail before a registry update could contradict facts already in the store."""

    for series in definitions.series:
        row = conn.execute(
            """
            SELECT observed_at, vintage_at, value, unit
            FROM observations
            WHERE series_id = ?
              AND (
                unit != ?
                OR (? IS NOT NULL AND value < ?)
                OR (? IS NOT NULL AND value > ?)
              )
            ORDER BY observed_at, vintage_at
            LIMIT 1
            """,
            (
                series.series_id,
                series.unit,
                series.plausible_min,
                series.plausible_min,
                series.plausible_max,
                series.plausible_max,
            ),
        ).fetchone()
        if row is None:
            continue
        if str(row["unit"]) != series.unit:
            detail = f"unit {row['unit']!r}; expected {series.unit!r}"
        else:
            low = "-inf" if series.plausible_min is None else f"{series.plausible_min:g}"
            high = "inf" if series.plausible_max is None else f"{series.plausible_max:g}"
            detail = f"value {float(row['value']):g} outside plausible range [{low}, {high}]"
        raise IndicatorsSchemaError(
            f"stored observation violates registry contract: {series.series_id} "
            f"{row['observed_at']} vintage {row['vintage_at']}: {detail}"
        )


def apply_registry_membership(
    conn: sqlite3.Connection,
    definitions: IndicatorDefinitions,
) -> None:
    """Replace the merge authorization set with one trusted registry snapshot."""

    conn.execute("DELETE FROM registry_series")
    conn.executemany(
        "INSERT INTO registry_series(series_id) VALUES (?)",
        ((series.series_id,) for series in definitions.series),
    )


def prune_definitions(
    conn: sqlite3.Connection,
    definitions: IndicatorDefinitions,
) -> tuple[RegistryPruneResult, ...]:
    """Delete facts for series absent from an explicitly trusted registry snapshot."""

    apply_registry_membership(conn, definitions)
    registered = {series.series_id for series in definitions.series}
    stored = {
        str(row["series_id"]) for row in conn.execute("SELECT series_id FROM series").fetchall()
    }
    results: list[RegistryPruneResult] = []
    for series_id in sorted(stored - registered):
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
    return tuple(results)


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

    if not db_path.exists():
        raise IndicatorsSchemaError(f"indicators store not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
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
) -> tuple[ObservationRecord, ...]:
    # ``point_in_time`` clamps to observations published on/before ``end`` so a
    # publish-lagged series stays point-in-time correct; otherwise the latest
    # vintage of each observed_at is returned regardless of publication date.
    end_text = end.isoformat()
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
        "AND inner_o.fetch_status = 'ok' "
        "AND (NOT ? OR substr(inner_o.vintage_at, 1, 10) <= ?)"
        ") ORDER BY o.observed_at",
        (series_id, start.isoformat(), end_text, pit, end_text, pit, end_text),
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
    pit = 1 if point_in_time else 0
    row = conn.execute(
        "SELECT o.* FROM observations o "
        "WHERE o.series_id = ? AND o.fetch_status = 'ok' "
        "AND (? IS NULL OR observed_at <= ?) "
        "AND (? IS NULL OR observed_at >= ?) "
        "AND (NOT ? OR ? IS NULL "
        "OR substr(o.vintage_at, 1, 10) <= ?) "
        "ORDER BY observed_at DESC, vintage_at DESC LIMIT 1",
        (
            series_id,
            cutoff,
            cutoff,
            on_or_after.isoformat() if on_or_after is not None else None,
            on_or_after.isoformat() if on_or_after is not None else None,
            pit,
            cutoff,
            cutoff,
        ),
    ).fetchone()
    return _observation_from_row(row) if row is not None else None


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version == SQLITE_SCHEMA_VERSION:
        _validate_trigger_contract(conn, schema="main")
        return
    if version == 1:
        conn.executescript(_MIGRATE_V1_TO_V2_SQL)
        version = 2
    if version == 2:
        conn.executescript(_MIGRATE_V2_TO_V3_SQL)
        version = 3
    if version == 3:
        conn.executescript(_MIGRATE_V3_TO_V4_SQL)
        _validate_trigger_contract(conn, schema="main")
        return
    if version != 0:
        raise IndicatorsSchemaError(
            f"unsupported indicator SQLite schema: {version}; expected {SQLITE_SCHEMA_VERSION}"
        )
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    _validate_trigger_contract(conn, schema="main")


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
