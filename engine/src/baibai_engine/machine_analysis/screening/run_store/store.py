"""Transactional publication boundary for prunable screening cache entries."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from baibai_engine.foundation.json import canonical_json
from baibai_engine.foundation.repository_layout import RUNS_DB_PATH
from baibai_engine.machine_analysis.screening.discovery.candidates import build_candidate_contexts
from baibai_engine.machine_analysis.screening.discovery.contracts import validate_candidate_run
from baibai_engine.machine_analysis.screening.rule_config import ScreeningRules
from baibai_engine.machine_analysis.screening.rules_identity import production_rules_contract_hash

from .schema import RUN_STORE_SCHEMA_VERSION, SCHEMA_SQL

DEFAULT_RUN_STORE_PATH = RUNS_DB_PATH


class RunStoreConflictError(ValueError):
    """Raised when immutable publication identity is reused with different content."""


@dataclass(frozen=True, slots=True)
class PublicationResult:
    publication_id: str
    inserted: bool


@dataclass(frozen=True, slots=True)
class PruneResult:
    kept_runs: int
    deleted_runs: int
    deleted_stock_analyses: int
    bytes_before: int
    bytes_after: int


def run_store_path(path: Path | None = None) -> Path:
    if path is not None:
        return path.expanduser()
    configured = os.environ.get("BAIBAI_RUNS_DB")
    return Path(configured).expanduser() if configured else DEFAULT_RUN_STORE_PATH


def connect_rw(path: Path | None = None) -> sqlite3.Connection:
    resolved = run_store_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(resolved, isolation_level=None)
    connection.row_factory = sqlite3.Row
    register_run_clock(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def run_clock(value: str) -> str:
    """Compare saved aware clocks as fixed-width UTC text at full microsecond precision."""
    return datetime.fromisoformat(value).astimezone(UTC).isoformat(timespec="microseconds")


def register_run_clock(connection: sqlite3.Connection) -> None:
    connection.create_function("run_clock", 1, run_clock, deterministic=True)


def initialize_run_store(path: Path | None = None) -> int:
    with closing(connect_rw(path)) as connection:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current == RUN_STORE_SCHEMA_VERSION:
            return current
        tables = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if current != 0 or tables is not None:
            raise RuntimeError(
                "screening run cache uses an obsolete schema; rebuild it "
                f"(found user_version={current}, expected={RUN_STORE_SCHEMA_VERSION})"
            )
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA_SQL)
            connection.execute(f"PRAGMA user_version = {RUN_STORE_SCHEMA_VERSION}")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        return RUN_STORE_SCHEMA_VERSION


class ScreeningRunStore:
    """The only writer for transactionally published run-cache entries."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self._path = path
        self._id_factory = id_factory

    def publish_run(
        self,
        payload: Mapping[str, object],
        *,
        rules: ScreeningRules,
        run_revision_id: str | None = None,
    ) -> PublicationResult:
        prepared = _prepare_run(payload, rules=rules)
        initialize_run_store(self._path)
        with closing(connect_rw(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = self._publish_run_in_transaction(
                    connection, prepared, run_revision_id=run_revision_id
                )
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def prune(self, *, keep: int = 3) -> PruneResult:
        if keep < 0:
            raise ValueError("keep must be zero or greater")
        initialize_run_store(self._path)
        path = run_store_path(self._path)
        bytes_before = path.stat().st_size
        deleted_stock_analyses = 0
        with closing(connect_rw(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                ordered = connection.execute(
                    """
                    SELECT run_revision_id FROM screening_run
                    ORDER BY asof_date DESC, run_clock(run_at) DESC, run_revision_id DESC
                    """
                ).fetchall()
                deleted_ids = [str(row[0]) for row in ordered[keep:]]
                if deleted_ids:
                    connection.execute(
                        """
                        CREATE TEMP TABLE prune_run_id (
                            run_revision_id TEXT PRIMARY KEY
                        ) WITHOUT ROWID
                        """
                    )
                    connection.executemany(
                        "INSERT INTO prune_run_id (run_revision_id) VALUES (?)",
                        ((run_revision_id,) for run_revision_id in deleted_ids),
                    )
                    deleted_stock_analyses = int(
                        connection.execute(
                            """
                            SELECT count(*) FROM stock_analysis
                            WHERE run_revision_id IN (SELECT run_revision_id FROM prune_run_id)
                            """
                        ).fetchone()[0]
                    )
                    connection.execute(
                        """
                        DELETE FROM stock_analysis
                        WHERE run_revision_id IN (SELECT run_revision_id FROM prune_run_id)
                        """
                    )
                    connection.execute(
                        """
                        DELETE FROM screening_run
                        WHERE run_revision_id IN (SELECT run_revision_id FROM prune_run_id)
                        """
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            try:
                connection.execute("VACUUM")
            except sqlite3.Error as error:
                raise sqlite3.OperationalError(
                    "run rows were pruned but VACUUM failed; rerun prune to compact the store"
                ) from error
        return PruneResult(
            kept_runs=min(keep, len(ordered)),
            deleted_runs=len(deleted_ids),
            deleted_stock_analyses=deleted_stock_analyses,
            bytes_before=bytes_before,
            bytes_after=path.stat().st_size,
        )

    def _publish_run_in_transaction(
        self,
        connection: sqlite3.Connection,
        prepared: _PreparedRun,
        *,
        run_revision_id: str | None = None,
    ) -> PublicationResult:
        existing = connection.execute(
            """
            SELECT run_revision_id, payload FROM screening_run
            WHERE public_run_id = ? AND run_at = ?
            """,
            (prepared.public_run_id, prepared.run_at),
        ).fetchone()
        if existing is not None:
            identifier = str(existing[0])
            if run_revision_id is not None and run_revision_id != identifier:
                raise RunStoreConflictError(
                    "run identity already belongs to a different run_revision_id"
                )
            stored_analyses = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT payload FROM stock_analysis
                    WHERE run_revision_id = ? ORDER BY ordinal
                    """,
                    (identifier,),
                ).fetchall()
            )
            expected_analyses = tuple(
                canonical_json(analysis.payload) for analysis in prepared.stock_analyses
            )
            if str(existing[1]) != prepared.payload_json or stored_analyses != expected_analyses:
                raise RunStoreConflictError(
                    f"run differs from existing immutable publication: {identifier}"
                )
            return PublicationResult(identifier, inserted=False)
        identifier = run_revision_id or f"run-revision-{self._id_factory().hex}"
        if connection.execute(
            "SELECT 1 FROM screening_run WHERE run_revision_id = ?", (identifier,)
        ).fetchone():
            raise RunStoreConflictError(f"run_revision_id already exists: {identifier}")
        connection.execute(
            """
            INSERT INTO screening_run (
                run_revision_id, public_run_id, run_date, asof_date, run_at,
                universe_size, rules_ref, created_at, payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identifier,
                prepared.public_run_id,
                prepared.run_date,
                prepared.asof_date,
                prepared.run_at,
                prepared.universe_size,
                prepared.rules_ref,
                datetime.now(UTC).isoformat(),
                prepared.payload_json,
            ),
        )
        for ordinal, analysis in enumerate(prepared.stock_analyses):
            connection.execute(
                """
                INSERT INTO stock_analysis (
                    run_revision_id, ordinal, ticker, sector_33, per_forward,
                    per_trailing, pbr, dividend_yield, er_annual, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    ordinal,
                    analysis.ticker,
                    _optional_string(analysis.payload.get("sector_33")),
                    _optional_number(analysis.payload.get("per_forward")),
                    _optional_number(analysis.payload.get("per_trailing")),
                    _optional_number(analysis.payload.get("pbr")),
                    _metric(analysis.payload, "dividend_yield"),
                    _metric(analysis.payload, "er_annual"),
                    canonical_json(analysis.payload),
                ),
            )
        return PublicationResult(identifier, inserted=True)


@dataclass(frozen=True, slots=True)
class _StockAnalysis:
    ticker: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class _PreparedRun:
    public_run_id: str
    run_date: str
    asof_date: str
    run_at: str
    universe_size: int
    rules_ref: str | None
    stock_analyses: tuple[_StockAnalysis, ...]
    payload_json: str


def _prepare_run(payload: Mapping[str, object], *, rules: ScreeningRules) -> _PreparedRun:
    if payload.get("screening_rules_hash") != production_rules_contract_hash(
        rules.model_dump_json()
    ):
        raise ValueError("screening_rules_hash does not match the supplied rules")
    if payload.get("schema_version") != 1 or payload.get("kind") != "stock_analysis_run":
        raise ValueError("expected stock_analysis_run schema_version 1")
    if "candidate_discovery" in payload:
        raise ValueError("candidate discovery is owned by the run writer")
    public_run_id = _required_string(payload, "run_id")
    run_date = _iso_date(payload, "run_date")
    asof_date = _iso_date(payload, "asof_date")
    if run_date != asof_date:
        raise ValueError("run_date must equal asof_date")
    if (
        public_run_id != f"screening-{asof_date.replace('-', '')}"
        or re.fullmatch(r"screening-[0-9]{8}", public_run_id) is None
    ):
        raise ValueError("run_id must be screening-YYYYMMDD for asof_date")
    run_at = _iso_datetime(payload, "run_at")
    universe_size = payload.get("universe_size")
    if isinstance(universe_size, bool) or not isinstance(universe_size, int) or universe_size < 0:
        raise ValueError("universe_size must be a non-negative integer")
    raw_analyses = payload.get("stock_analyses")
    if not isinstance(raw_analyses, Sequence) or isinstance(raw_analyses, (str, bytes)):
        raise ValueError("stock_analyses must be an array")
    stock_analyses: list[_StockAnalysis] = []
    tickers: set[str] = set()
    for raw in raw_analyses:
        if not isinstance(raw, Mapping):
            raise ValueError("stock analysis must be a mapping")
        ticker = _required_string(raw, "ticker")
        if re.fullmatch(r"[0-9A-Z]{4}", ticker) is None:
            raise ValueError(f"stock analysis ticker has invalid format: {ticker}")
        if ticker in tickers:
            raise RunStoreConflictError(f"duplicate stock analysis ticker: {ticker}")
        _required_string(raw, "name")
        if raw.get("candidate") is not None:
            raise ValueError("candidate context is owned by the run writer")
        tickers.add(ticker)
        stock_analyses.append(_StockAnalysis(ticker=ticker, payload=dict(raw)))
    rules_ref = payload.get("rules_ref")
    if rules_ref is not None and not isinstance(rules_ref, str):
        raise ValueError("rules_ref must be a string or null")
    for identity_key in ("screening_rules_hash", "er_model_version"):
        identity_value = payload.get(identity_key)
        if identity_value is not None and (
            not isinstance(identity_value, str) or not identity_value.strip()
        ):
            raise ValueError(f"{identity_key} must be a non-empty string or null")
    discovery = build_candidate_contexts(
        [analysis.payload for analysis in stock_analyses],
        rules=rules.candidate_discovery,
        required_jpx_flags=rules.universe.required_jpx_flags,
    )
    stock_analyses = [
        _StockAnalysis(
            ticker=analysis.ticker,
            payload={
                **analysis.payload,
                "candidate": discovery.contexts[analysis.ticker].model_dump(mode="json"),
            },
        )
        for analysis in stock_analyses
    ]
    header = {
        key: value
        for key, value in payload.items()
        if key not in {"stock_analyses", "run_revision_id"}
    }
    header["candidate_discovery"] = discovery.summary_payload()
    validate_candidate_run(header, [analysis.payload for analysis in stock_analyses])
    return _PreparedRun(
        public_run_id=public_run_id,
        run_date=run_date,
        asof_date=asof_date,
        run_at=run_at,
        universe_size=universe_size,
        rules_ref=rules_ref,
        stock_analyses=tuple(stock_analyses),
        payload_json=canonical_json(header),
    )


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _iso_date(payload: Mapping[str, object], key: str) -> str:
    value = _required_string(payload, key)
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError(f"{key} must be an ISO date") from error


def _iso_datetime(payload: Mapping[str, object], key: str) -> str:
    value = _required_string(payload, key)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{key} must be an ISO datetime") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{key} must be timezone-aware")
    return value


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_number(value: object) -> float | int | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _metric(payload: Mapping[str, object], key: str) -> float | int | None:
    metrics = payload.get("metrics")
    if not isinstance(metrics, Mapping):
        return None
    return _optional_number(metrics.get(key))


def decode_payload(value: object) -> Mapping[str, Any]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise sqlite3.DatabaseError("run store payload is not an object")
    return decoded


__all__ = [
    "DEFAULT_RUN_STORE_PATH",
    "PruneResult",
    "PublicationResult",
    "RunStoreConflictError",
    "ScreeningRunStore",
    "connect_rw",
    "decode_payload",
    "initialize_run_store",
    "run_store_path",
]
