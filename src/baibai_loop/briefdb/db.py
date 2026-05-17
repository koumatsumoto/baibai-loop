from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .definitions import (
    BriefDBDefinitions,
    IndicatorDefinition,
    SourceDefinition,
    load_definitions,
)

SQLITE_SCHEMA_VERSION = 1
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path("data/brief/macro.sqlite")


class BriefDBSchemaError(RuntimeError):
    """Raised when the brief SQLite database is missing or out of date."""


@dataclass(frozen=True)
class ObservationRecord:
    indicator_id: str
    period_start: str | None
    period_end: str | None
    as_of_date: str | None
    release_date: str | None
    value_num: float | None
    value_text: str | None
    unit: str | None
    source_id: str | None
    source_url: str | None
    fetched_at: str | None
    vintage_at: str | None
    fetch_status: str
    raw_payload_hash: str | None
    brief_path: str | None
    note: str | None

    @property
    def observation_id(self) -> str:
        parts = (
            self.indicator_id,
            self.period_start or "",
            self.period_end or "",
            self.as_of_date or "",
            self.release_date or "",
            self.source_id or "",
            self.vintage_at or "",
        )
        digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]
        return f"obs-{digest}"


def initialize_database(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    definitions: BriefDBDefinitions | None = None,
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
    except BaseException:
        conn.close()
        raise
    return conn


def validate_current_schema(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version != SQLITE_SCHEMA_VERSION:
        raise BriefDBSchemaError(
            f"unsupported brief SQLite schema: {version}; expected {SQLITE_SCHEMA_VERSION}"
        )


def seed_definitions(conn: sqlite3.Connection, definitions: BriefDBDefinitions) -> None:
    for source in definitions.sources:
        upsert_source(conn, source)
    for indicator in definitions.indicators:
        upsert_indicator(conn, indicator)
    for requirement in definitions.coverage_requirements:
        conn.execute(
            "INSERT OR REPLACE INTO coverage_requirements("
            "kind, indicator_id, required, max_staleness_days"
            ") VALUES (?, ?, ?, ?)",
            (
                requirement.kind,
                requirement.indicator_id,
                1 if requirement.required else 0,
                requirement.max_staleness_days,
            ),
        )


def upsert_source(conn: sqlite3.Connection, source: SourceDefinition) -> None:
    conn.execute(
        "INSERT INTO sources("
        "source_id, name, provider, tier, url, access_method, status_policy, note"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source_id) DO UPDATE SET "
        "name = excluded.name, "
        "provider = COALESCE(excluded.provider, sources.provider), "
        "tier = COALESCE(excluded.tier, sources.tier), "
        "url = COALESCE(excluded.url, sources.url), "
        "access_method = COALESCE(excluded.access_method, sources.access_method), "
        "status_policy = COALESCE(excluded.status_policy, sources.status_policy), "
        "note = COALESCE(excluded.note, sources.note)",
        (
            source.source_id,
            source.name,
            source.provider,
            source.tier,
            source.url,
            source.access_method,
            source.status_policy,
            source.note,
        ),
    )


def upsert_indicator(conn: sqlite3.Connection, indicator: IndicatorDefinition) -> None:
    conn.execute(
        "INSERT INTO indicators("
        "indicator_id, name, domain, geography, unit, frequency, primary_source_id, "
        "transform_rule, description"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(indicator_id) DO UPDATE SET "
        "name = excluded.name, "
        "domain = excluded.domain, "
        "geography = excluded.geography, "
        "unit = COALESCE(excluded.unit, indicators.unit), "
        "frequency = excluded.frequency, "
        "primary_source_id = COALESCE(excluded.primary_source_id, indicators.primary_source_id), "
        "transform_rule = excluded.transform_rule, "
        "description = COALESCE(excluded.description, indicators.description)",
        (
            indicator.indicator_id,
            indicator.name,
            indicator.domain,
            indicator.geography,
            indicator.unit,
            indicator.frequency,
            indicator.primary_source_id,
            indicator.transform_rule,
            indicator.description,
        ),
    )
    conn.execute(
        "INSERT OR REPLACE INTO indicator_aliases(alias, indicator_id) VALUES (?, ?)",
        (indicator.name, indicator.indicator_id),
    )
    for alias in indicator.aliases:
        conn.execute(
            "INSERT OR REPLACE INTO indicator_aliases(alias, indicator_id) VALUES (?, ?)",
            (alias, indicator.indicator_id),
        )


def insert_observation(conn: sqlite3.Connection, observation: ObservationRecord) -> None:
    conn.execute(
        "INSERT INTO indicator_observations("
        "observation_id, indicator_id, period_start, period_end, as_of_date, release_date, "
        "value_num, value_text, unit, source_id, source_url, fetched_at, vintage_at, "
        "fetch_status, raw_payload_hash, brief_path, note"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(observation_id) DO UPDATE SET "
        "value_num = excluded.value_num, "
        "value_text = excluded.value_text, "
        "unit = COALESCE(excluded.unit, indicator_observations.unit), "
        "source_url = COALESCE(excluded.source_url, indicator_observations.source_url), "
        "fetched_at = COALESCE(excluded.fetched_at, indicator_observations.fetched_at), "
        "fetch_status = excluded.fetch_status, "
        "raw_payload_hash = COALESCE(excluded.raw_payload_hash, "
        "indicator_observations.raw_payload_hash), "
        "brief_path = COALESCE(excluded.brief_path, indicator_observations.brief_path), "
        "note = COALESCE(excluded.note, indicator_observations.note)",
        (
            observation.observation_id,
            observation.indicator_id,
            observation.period_start,
            observation.period_end,
            observation.as_of_date,
            observation.release_date,
            observation.value_num,
            observation.value_text,
            observation.unit,
            observation.source_id,
            observation.source_url,
            observation.fetched_at,
            observation.vintage_at,
            observation.fetch_status,
            observation.raw_payload_hash,
            observation.brief_path,
            observation.note,
        ),
    )


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
        raise BriefDBSchemaError(
            f"unsupported brief SQLite schema: {version}; expected {SQLITE_SCHEMA_VERSION}"
        )
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
