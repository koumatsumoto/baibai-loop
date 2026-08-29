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

from baibai_engine.appdb.json import canonical_json
from baibai_engine.foundation.repository_layout import RUNS_DB_PATH
from baibai_engine.screening.selection.contracts import (
    SelectionContractError,
    expected_ranked_set,
    validate_selection_payload,
)

from .schema import RUN_STORE_SCHEMA_VERSION, SCHEMA_SQL

DEFAULT_RUN_STORE_PATH = RUNS_DB_PATH


class RunStoreConflictError(ValueError):
    """Raised when immutable publication identity is reused with different content."""


class RunStoreNotFoundError(ValueError):
    """Raised when a publication refers to an unknown parent."""


class RunStoreAmbiguousError(ValueError):
    """Raised when a convenience query cannot identify one immutable revision."""


@dataclass(frozen=True, slots=True)
class PublicationResult:
    publication_id: str
    inserted: bool


@dataclass(frozen=True, slots=True)
class PruneResult:
    kept_runs: int
    deleted_runs: int
    deleted_candidates: int
    deleted_selections: int
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
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


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
        connection.executescript(SCHEMA_SQL)
        connection.execute(f"PRAGMA user_version = {RUN_STORE_SCHEMA_VERSION}")
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
        run_revision_id: str | None = None,
    ) -> PublicationResult:
        prepared = _prepare_run(payload)
        initialize_run_store(self._path)
        with closing(connect_rw(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = self._publish_run_in_transaction(
                    connection,
                    prepared,
                    run_revision_id=run_revision_id,
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
        deleted_candidates = 0
        deleted_selections = 0
        with closing(connect_rw(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                ordered = connection.execute(
                    """
                    SELECT run_revision_id FROM screening_run
                    ORDER BY asof_date DESC, run_at DESC, run_revision_id DESC
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
                    deleted_candidates = int(
                        connection.execute(
                            """
                            SELECT count(*) FROM screening_candidate
                            WHERE run_revision_id IN (SELECT run_revision_id FROM prune_run_id)
                            """
                        ).fetchone()[0]
                    )
                    deleted_selections = int(
                        connection.execute(
                            """
                            SELECT count(*) FROM screening_selection
                            WHERE run_revision_id IN (SELECT run_revision_id FROM prune_run_id)
                            """
                        ).fetchone()[0]
                    )
                    connection.execute(
                        """
                        DELETE FROM screening_selection
                        WHERE run_revision_id IN (SELECT run_revision_id FROM prune_run_id)
                        """
                    )
                    connection.execute(
                        """
                        DELETE FROM screening_candidate
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
            deleted_candidates=deleted_candidates,
            deleted_selections=deleted_selections,
            bytes_before=bytes_before,
            bytes_after=path.stat().st_size,
        )

    def publish_selection(
        self,
        *,
        run_revision_id: str,
        macro_context_id: str | None,
        payload: Mapping[str, object],
        selection_id: str | None = None,
        created_at: datetime | None = None,
    ) -> PublicationResult:
        if not run_revision_id:
            raise ValueError("run_revision_id is required")
        policy_parameters = validate_selection_payload(payload)
        identifier = selection_id or f"selection-{self._id_factory().hex}"
        timestamp = (created_at or datetime.now(UTC)).isoformat()
        payload_json = canonical_json(payload)
        initialize_run_store(self._path)
        with closing(connect_rw(self._path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                run_row = connection.execute(
                    "SELECT payload FROM screening_run WHERE run_revision_id = ?",
                    (run_revision_id,),
                ).fetchone()
                if run_row is None:
                    raise RunStoreNotFoundError(f"unknown run_revision_id: {run_revision_id}")
                run_payload = decode_payload(run_row[0])
                candidate_rows = connection.execute(
                    """
                    SELECT ticker, er_annual, payload FROM screening_candidate
                    WHERE run_revision_id = ?
                    """,
                    (run_revision_id,),
                ).fetchall()
                _validate_selection_run_binding(
                    payload,
                    run_payload,
                    run_revision_id=run_revision_id,
                    macro_context_id=macro_context_id,
                    source_candidate_er={str(row[0]): row[1] for row in candidate_rows},
                    expected_ranked_set=expected_ranked_set(
                        [decode_payload(row[2]) for row in candidate_rows],
                        parameters=policy_parameters,
                        asof_date=str(run_payload.get("asof_date")),
                    ),
                )
                existing = connection.execute(
                    """
                    SELECT run_revision_id, macro_context_id, payload
                    FROM screening_selection WHERE selection_id = ?
                    """,
                    (identifier,),
                ).fetchone()
                expected = (
                    run_revision_id,
                    macro_context_id,
                    payload_json,
                )
                if existing is not None:
                    actual = tuple(existing)
                    if actual != expected:
                        raise RunStoreConflictError(
                            f"selection differs from existing publication: {identifier}"
                        )
                    connection.rollback()
                    return PublicationResult(identifier, inserted=False)
                connection.execute(
                    """
                    INSERT INTO screening_selection (
                        selection_id, run_revision_id, macro_context_id, created_at, payload
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        run_revision_id,
                        macro_context_id,
                        timestamp,
                        payload_json,
                    ),
                )
                connection.commit()
                return PublicationResult(identifier, inserted=True)
            except BaseException:
                connection.rollback()
                raise

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
            stored_candidates = tuple(
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT payload FROM screening_candidate
                    WHERE run_revision_id = ? ORDER BY ordinal
                    """,
                    (identifier,),
                ).fetchall()
            )
            expected_candidates = tuple(
                canonical_json(candidate.payload) for candidate in prepared.candidates
            )
            if (
                str(existing[1]) != prepared.payload_json
                or stored_candidates != expected_candidates
            ):
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
        for ordinal, candidate in enumerate(prepared.candidates):
            connection.execute(
                """
                INSERT INTO screening_candidate (
                    run_revision_id, ordinal, ticker, sector_33, per_forward,
                    per_trailing, pbr, dividend_yield, er_annual, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    ordinal,
                    candidate.ticker,
                    _optional_string(candidate.payload.get("sector_33")),
                    _optional_number(candidate.payload.get("per_forward")),
                    _optional_number(candidate.payload.get("per_trailing")),
                    _optional_number(candidate.payload.get("pbr")),
                    _metric(candidate.payload, "dividend_yield"),
                    _metric(candidate.payload, "er_annual"),
                    canonical_json(candidate.payload),
                ),
            )
        return PublicationResult(identifier, inserted=True)


@dataclass(frozen=True, slots=True)
class _Candidate:
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
    candidates: tuple[_Candidate, ...]
    payload_json: str


def _prepare_run(payload: Mapping[str, object]) -> _PreparedRun:
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
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, Sequence) or isinstance(raw_candidates, (str, bytes)):
        raise ValueError("candidates must be an array")
    candidates: list[_Candidate] = []
    tickers: set[str] = set()
    for raw in raw_candidates:
        if not isinstance(raw, Mapping):
            raise ValueError("candidate must be a mapping")
        ticker = _required_string(raw, "ticker")
        if re.fullmatch(r"[0-9A-Z]{4}", ticker) is None:
            raise ValueError(f"candidate ticker has invalid format: {ticker}")
        if ticker in tickers:
            raise RunStoreConflictError(f"duplicate candidate ticker: {ticker}")
        _required_string(raw, "name")
        _validate_evidence_hits(raw, ticker=ticker)
        tickers.add(ticker)
        candidates.append(_Candidate(ticker=ticker, payload=dict(raw)))
    rules_ref = payload.get("rules_ref")
    if rules_ref is not None and not isinstance(rules_ref, str):
        raise ValueError("rules_ref must be a string or null")
    for identity_key in ("screening_rules_hash", "er_model_version"):
        identity_value = payload.get(identity_key)
        if identity_value is not None and (
            not isinstance(identity_value, str) or not identity_value.strip()
        ):
            raise ValueError(f"{identity_key} must be a non-empty string or null")
    evidence_summary = payload.get("evidence_hits_summary")
    if evidence_summary is not None:
        if not isinstance(evidence_summary, Mapping):
            raise ValueError("evidence_hits_summary must be an object")
        for name, count in evidence_summary.items():
            if (
                not isinstance(name, str)
                or not name
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                raise ValueError("evidence_hits_summary values must be non-negative integers")
    return _PreparedRun(
        public_run_id=public_run_id,
        run_date=run_date,
        asof_date=asof_date,
        run_at=run_at,
        universe_size=universe_size,
        rules_ref=rules_ref,
        candidates=tuple(candidates),
        payload_json=canonical_json(
            {key: value for key, value in payload.items() if key != "candidates"}
        ),
    )


def _validate_selection_run_binding(
    selection_payload: Mapping[str, object],
    run_payload: Mapping[str, object],
    *,
    run_revision_id: str,
    macro_context_id: str | None,
    source_candidate_er: Mapping[str, object],
    expected_ranked_set: Sequence[tuple[str, float, str | None, Mapping[str, object]]],
) -> None:
    selection = selection_payload.get("selection")
    if not isinstance(selection, Mapping):
        raise SelectionContractError("selection metadata must be an object")
    for key in ("screening_rules_hash", "er_model_version"):
        run_value = run_payload.get(key)
        selection_value = selection.get(key)
        if not isinstance(run_value, str) or not run_value.strip():
            raise SelectionContractError(f"source run has no exact {key}")
        if selection_value != run_value:
            raise SelectionContractError(f"selection {key} does not match the source run")
    if selection.get("asof") != run_payload.get("asof_date"):
        raise SelectionContractError("selection asof does not match the source run")
    input_refs = selection.get("input_refs")
    if not isinstance(input_refs, Mapping):
        raise SelectionContractError("selection input_refs must be an object")
    if input_refs.get("candidates_ref") != run_revision_id:
        raise SelectionContractError("selection candidates_ref does not match the source run")
    if input_refs.get("macro_context_ref") != macro_context_id:
        raise SelectionContractError("selection macro_context_ref does not match the publication")
    raw_ranked_set = selection_payload.get("ranked_set")
    if not isinstance(raw_ranked_set, Sequence) or isinstance(raw_ranked_set, str | bytes):
        raise SelectionContractError("ranked_set must be an array")
    actual: list[tuple[str, float, str | None]] = []
    validated: list[Mapping[str, object]] = []
    for row in raw_ranked_set:
        if not isinstance(row, Mapping):  # pragma: no cover - checked by contract validation
            raise SelectionContractError("ranked-set row must be an object")
        ticker = row.get("ticker")
        if not isinstance(ticker, str) or ticker not in source_candidate_er:
            raise SelectionContractError("ranked-set ticker does not belong to the source run")
        if row.get("er_annual") != source_candidate_er[ticker]:
            raise SelectionContractError("ranked-set E[r] does not match the source run")
        primary_pattern = row.get("primary_evidence_pattern_id")
        if primary_pattern is not None and not isinstance(primary_pattern, str):
            raise SelectionContractError(
                "ranked-set primary Evidence Pattern ID must be a string or null"
            )
        actual.append((ticker, float(row["er_annual"]), primary_pattern))
        validated.append(row)
    expected_rank_coordinates = tuple(row[:3] for row in expected_ranked_set)
    if tuple(actual) != expected_rank_coordinates:
        raise SelectionContractError(
            "ranked-set order or Evidence Pattern does not match the selection method"
        )
    for row, expected in zip(validated, expected_ranked_set, strict=True):
        for key, expected_value in expected[3].items():
            if row.get(key) != expected_value:
                raise SelectionContractError(
                    f"ranked-set {key} does not match the source candidate"
                )


def _validate_evidence_hits(candidate: Mapping[str, object], *, ticker: str) -> None:
    hits = candidate.get("evidence_hits")
    if not isinstance(hits, Sequence) or isinstance(hits, (str, bytes)):
        raise ValueError(f"candidate evidence_hits must be an array: {ticker}")
    allowed_statuses = {"ok", "warning", "stale", "missing"}
    for hit in hits:
        if not isinstance(hit, Mapping):
            raise ValueError(f"candidate evidence hit must be an object: {ticker}")
        _required_string(hit, "name")
        evidence_pattern_id = hit.get("evidence_pattern_id")
        if not isinstance(evidence_pattern_id, str) or not evidence_pattern_id.strip():
            raise ValueError(f"evidence pattern ID is missing: {ticker}")
        status = hit.get("source_status")
        eligible = hit.get("sizing_eligible")
        if status not in allowed_statuses:
            raise ValueError(f"evidence source_status is invalid: {ticker}")
        if not isinstance(eligible, bool):
            raise ValueError(f"evidence sizing_eligible must be boolean: {ticker}")
        if status != "ok" and eligible:
            raise ValueError(f"non-ok evidence cannot be sizing eligible: {ticker}")


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
    "RunStoreAmbiguousError",
    "RunStoreConflictError",
    "RunStoreNotFoundError",
    "ScreeningRunStore",
    "connect_rw",
    "decode_payload",
    "initialize_run_store",
    "run_store_path",
]
