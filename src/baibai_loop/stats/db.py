from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast

from .definitions import SeriesDefinition, StatsDefinitions, load_definitions

SQLITE_SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path("data/stats/macro.sqlite")


class StatsSchemaError(RuntimeError):
    """Raised when the stats SQLite schema is missing or unsupported."""


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


def initialize_database(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    definitions: StatsDefinitions | None = None,
) -> sqlite3.Connection:
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        seed_definitions(conn, definitions or load_definitions())
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def open_connection(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    if not db_path.exists():
        return initialize_database(db_path)
    conn = _connect(db_path)
    try:
        validate_current_schema(conn)
        seed_definitions(conn, load_definitions())
        conn.commit()
    except BaseException:
        conn.close()
        raise
    return conn


def validate_current_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != SQLITE_SCHEMA_VERSION:
        raise StatsSchemaError(
            f"unsupported stats SQLite schema: {version}; expected {SQLITE_SCHEMA_VERSION}"
        )


def seed_definitions(conn: sqlite3.Connection, definitions: StatsDefinitions) -> None:
    series_ids = tuple(series.series_id for series in definitions.series)
    conn.execute("DELETE FROM aliases")
    _delete_rows_not_in(conn, "provider_runs", "series_id", series_ids)
    _delete_rows_not_in(conn, "observations", "series_id", series_ids)
    _delete_rows_not_in(conn, "series", "series_id", series_ids)
    for series in definitions.series:
        conn.execute(
            "INSERT INTO series("
            "series_id, name, domain, geography, frequency, unit, provider, provider_series_id, "
            "source_id, source_url, priority, notes"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(series_id) DO UPDATE SET "
            "name = excluded.name, domain = excluded.domain, geography = excluded.geography, "
            "frequency = excluded.frequency, unit = excluded.unit, provider = excluded.provider, "
            "provider_series_id = excluded.provider_series_id, source_id = excluded.source_id, "
            "source_url = excluded.source_url, priority = excluded.priority, "
            "notes = excluded.notes",
            (
                series.series_id,
                series.name,
                series.domain,
                series.geography,
                series.frequency,
                series.unit,
                series.provider,
                series.provider_series_id,
                series.source_id,
                series.source_url,
                series.priority,
                series.notes,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO aliases(alias, series_id) VALUES (?, ?)",
            (series.name, series.series_id),
        )
        for alias in series.aliases:
            conn.execute(
                "INSERT OR REPLACE INTO aliases(alias, series_id) VALUES (?, ?)",
                (alias, series.series_id),
            )


def get_series(conn: sqlite3.Connection, series_id: str) -> SeriesDefinition:
    row = conn.execute("SELECT * FROM series WHERE series_id = ?", (series_id,)).fetchone()
    if row is None:
        raise KeyError(f"unknown stats series: {series_id}")
    alias_rows = conn.execute(
        "SELECT alias FROM aliases WHERE series_id = ? ORDER BY alias",
        (series_id,),
    ).fetchall()
    return _series_from_row(row, aliases=tuple(str(item["alias"]) for item in alias_rows))


def list_series(
    conn: sqlite3.Connection, *, domain: str | None = None
) -> tuple[SeriesDefinition, ...]:
    params: tuple[str, ...] = ()
    sql = "SELECT * FROM series"
    if domain is not None:
        sql += " WHERE domain = ?"
        params = (domain,)
    sql += " ORDER BY priority, series_id"
    return tuple(_series_from_row(row, aliases=()) for row in conn.execute(sql, params).fetchall())


def search_series(conn: sqlite3.Connection, query: str) -> tuple[SeriesDefinition, ...]:
    needle = f"%{query.casefold()}%"
    rows = conn.execute(
        "SELECT DISTINCT s.* FROM series s "
        "LEFT JOIN aliases a ON a.series_id = s.series_id "
        "WHERE lower(s.series_id) LIKE ? OR lower(s.name) LIKE ? "
        "OR lower(s.domain) LIKE ? OR lower(s.geography) LIKE ? OR lower(a.alias) LIKE ? "
        "ORDER BY s.priority, s.series_id",
        (needle, needle, needle, needle, needle),
    ).fetchall()
    return tuple(_series_from_row(row, aliases=()) for row in rows)


def insert_observations(
    conn: sqlite3.Connection,
    observations: list[ObservationRecord],
) -> None:
    conn.executemany(
        "INSERT INTO observations("
        "series_id, observed_at, period_start, period_end, value, unit, vintage_at, "
        "fetch_status, source_url"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(series_id, observed_at, vintage_at) DO UPDATE SET "
        "period_start = excluded.period_start, period_end = excluded.period_end, "
        "value = excluded.value, unit = excluded.unit, fetch_status = excluded.fetch_status, "
        "source_url = excluded.source_url",
        [
            (
                item.series_id,
                item.observed_at.isoformat(),
                item.period_start.isoformat() if item.period_start else None,
                item.period_end.isoformat() if item.period_end else None,
                item.value,
                item.unit,
                (item.vintage_at or datetime.now(UTC)).isoformat(),
                item.fetch_status,
                item.source_url,
            )
            for item in observations
        ],
    )


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
        "AND date(range_start) <= date(?) AND date(range_end) >= date(?) "
        "ORDER BY finished_at DESC LIMIT 1",
        (series_id, start.isoformat(), end.isoformat()),
    ).fetchone()
    return row is not None


def observations_in_range(
    conn: sqlite3.Connection,
    series_id: str,
    start: date,
    end: date,
) -> tuple[ObservationRecord, ...]:
    rows = conn.execute(
        "SELECT o.* FROM observations o WHERE o.series_id = ? "
        "AND date(o.observed_at) BETWEEN date(?) AND date(?) "
        "AND o.fetch_status = 'ok' "
        "AND o.vintage_at = ("
        "SELECT MAX(inner_o.vintage_at) FROM observations inner_o "
        "WHERE inner_o.series_id = o.series_id "
        "AND inner_o.observed_at = o.observed_at "
        "AND inner_o.fetch_status = 'ok'"
        ") ORDER BY date(o.observed_at)",
        (series_id, start.isoformat(), end.isoformat()),
    ).fetchall()
    return tuple(_observation_from_row(row) for row in rows)


def latest_observation(
    conn: sqlite3.Connection,
    series_id: str,
    *,
    on_or_before: date | None = None,
    on_or_after: date | None = None,
) -> ObservationRecord | None:
    clauses = ["series_id = ?", "fetch_status = 'ok'"]
    params: list[str] = [series_id]
    if on_or_before is not None:
        clauses.append("date(observed_at) <= date(?)")
        params.append(on_or_before.isoformat())
    if on_or_after is not None:
        clauses.append("date(observed_at) >= date(?)")
        params.append(on_or_after.isoformat())
    row = conn.execute(
        "SELECT * FROM observations WHERE "
        + " AND ".join(clauses)
        + " ORDER BY date(observed_at) DESC, vintage_at DESC LIMIT 1",
        tuple(params),
    ).fetchone()
    return _observation_from_row(row) if row is not None else None


def previous_observation(
    conn: sqlite3.Connection,
    series_id: str,
    *,
    before: date,
) -> ObservationRecord | None:
    row = conn.execute(
        "SELECT * FROM observations WHERE series_id = ? AND fetch_status = 'ok' "
        "AND date(observed_at) < date(?) "
        "ORDER BY date(observed_at) DESC, vintage_at DESC LIMIT 1",
        (series_id, before.isoformat()),
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
        return
    if version != 0:
        raise StatsSchemaError(
            f"unsupported stats SQLite schema: {version}; expected {SQLITE_SCHEMA_VERSION}"
        )
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


def _series_from_row(row: sqlite3.Row, *, aliases: tuple[str, ...]) -> SeriesDefinition:
    return SeriesDefinition(
        series_id=str(row["series_id"]),
        name=str(row["name"]),
        domain=str(row["domain"]),
        geography=str(row["geography"]),
        frequency=str(row["frequency"]),
        unit=str(row["unit"]),
        provider=str(row["provider"]),
        provider_series_id=str(row["provider_series_id"]),
        source_id=str(row["source_id"]),
        source_url=str(row["source_url"]),
        priority=int(row["priority"]),
        notes=str(row["notes"]) if row["notes"] is not None else None,
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
    if table not in {"series", "aliases", "observations", "provider_runs"}:
        raise ValueError(f"unsupported stats table: {table}")
    return cast(int, conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _delete_rows_not_in(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    values: tuple[str, ...],
) -> None:
    if table not in {"series", "observations", "provider_runs"}:
        raise ValueError(f"unsupported stats table: {table}")
    if column != "series_id":
        raise ValueError(f"unsupported stats column: {column}")
    if not values:
        conn.execute(f"DELETE FROM {table}")
        return
    placeholders = ",".join("?" for _ in values)
    conn.execute(f"DELETE FROM {table} WHERE {column} NOT IN ({placeholders})", values)
