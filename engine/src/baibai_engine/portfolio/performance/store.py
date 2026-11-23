"""Immutable application-DB publications for portfolio outcomes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from math import isfinite
from pathlib import Path

from baibai_engine.portfolio.performance.contracts import PortfolioOutcomePayload
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.write import connect_existing, initialize_database
from baibai_engine.storage.sqlite.read import read_rows

_OUTCOME_COORDINATES = ("horizon", "period_start_date", "period_end_date", "status")
_OUTCOME_SELECT = (
    "SELECT outcome_id, horizon, period_start_date, period_end_date, status, payload "
    "FROM portfolio_outcome "
)


class PortfolioOutcomeConflictError(ValueError):
    """An outcome ID already names different immutable content."""


@dataclass(frozen=True, slots=True)
class PortfolioOutcomePublication:
    outcome_id: str
    horizon: str
    period_start_date: str
    period_end_date: str
    status: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PortfolioOutcomeReceipt:
    outcome_id: str
    payload_sha256: str
    created: bool


@dataclass(frozen=True, slots=True)
class SavedPortfolioOutcome:
    payload: dict[str, object]
    payload_sha256: str


class PortfolioOutcomeStore:
    """Publish and query immutable outcome records."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def publish(self, publication: PortfolioOutcomePublication) -> PortfolioOutcomeReceipt:
        encoded = canonical_json(publication.payload)
        # Validate the saved representation, not the types before JSON encoding.
        payload = _validate(publication, json.loads(encoded))
        digest = _payload_sha256(encoded)
        initialize_database(self._db_path)
        with closing(connect_existing(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT horizon, period_start_date, period_end_date, status, payload "
                    "FROM portfolio_outcome WHERE outcome_id = ?",
                    (publication.outcome_id,),
                ).fetchone()
                expected = (
                    publication.horizon,
                    publication.period_start_date,
                    publication.period_end_date,
                    publication.status,
                    encoded,
                )
                if existing is not None:
                    if tuple(existing) != expected:
                        raise PortfolioOutcomeConflictError(
                            f"outcome_id has different content: {publication.outcome_id}"
                        )
                    connection.commit()
                    return PortfolioOutcomeReceipt(publication.outcome_id, digest, created=False)
                # Preserve exact retries of results saved before this input check.
                payload.benchmark_observation.require_completed_period()
                connection.execute(
                    "INSERT INTO portfolio_outcome "
                    "(outcome_id, horizon, period_start_date, period_end_date, status, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (publication.outcome_id, *expected),
                )
                connection.commit()
                return PortfolioOutcomeReceipt(publication.outcome_id, digest, created=True)
            except BaseException:
                connection.rollback()
                raise

    def get(self, outcome_id: str) -> SavedPortfolioOutcome | None:
        """Read one immutable result without recalculating it from current inputs."""
        rows = read_rows(
            database_path(self._db_path),
            _OUTCOME_SELECT + "WHERE outcome_id = ?",
            (outcome_id,),
            connector=connect_read_only,
        )
        if not rows:
            return None
        row = rows[0]
        return SavedPortfolioOutcome(_saved_payload(row), _payload_sha256(row["payload"]))

    def list(self) -> tuple[Mapping[str, object], ...]:
        rows = read_rows(
            database_path(self._db_path),
            _OUTCOME_SELECT + "ORDER BY period_end_date DESC, outcome_id DESC",
            connector=connect_read_only,
        )
        return tuple(_saved_payload(row) for row in rows)


def _payload_sha256(encoded: str) -> str:
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _require_outcome_period(start: str, end: str) -> None:
    try:
        if all(date.fromisoformat(value).isoformat() == value for value in (start, end)):
            return
    except ValueError:
        pass
    raise ValueError("portfolio outcome period dates must be canonical ISO dates")


def _outcome_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload = dict(pairs)
    if len(payload) != len(pairs):
        raise ValueError("saved portfolio outcome contains a duplicate JSON field")
    return payload


def _outcome_json_float(token: str) -> float:
    value = float(token)
    if not isfinite(value):
        raise ValueError("saved portfolio outcome contains a non-finite JSON number")
    return value


def _saved_payload(row: sqlite3.Row) -> dict[str, object]:
    try:
        payload = json.loads(
            row["payload"],
            object_pairs_hook=_outcome_json_object,
            parse_float=_outcome_json_float,
            parse_constant=_outcome_json_float,
        )
    except (json.JSONDecodeError, RecursionError):
        raise ValueError("saved portfolio outcome payload is not valid JSON") from None
    outcome_id = row["outcome_id"]
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "portfolio_outcome"
        or payload.get("outcome_id", outcome_id) != outcome_id
        or any(payload.get(field) != row[field] for field in _OUTCOME_COORDINATES)
    ):
        raise ValueError("saved portfolio outcome identity does not match its row")
    _require_outcome_period(row["period_start_date"], row["period_end_date"])
    version = payload.get("schema_version")
    if type(version) is not int or version not in (1, 2):
        raise ValueError("unrecognized saved portfolio outcome schema")
    # Retained V1 is read as-is. For V2, check the stored facts, not the newer
    # publication-time clock or the current ledger, market, or benchmark inputs.
    if version == 2:
        try:
            PortfolioOutcomePayload.model_validate(payload)
        except ValueError:
            raise ValueError("invalid stored content for saved portfolio outcome") from None
    return {**payload, "outcome_id": outcome_id}


def _validate(
    publication: PortfolioOutcomePublication, payload: Mapping[str, object]
) -> PortfolioOutcomePayload:
    if type(payload.get("schema_version")) is not int:
        raise ValueError("outcome payload schema_version must be an integer")
    validated = PortfolioOutcomePayload.model_validate(payload)
    for field in _OUTCOME_COORDINATES:
        if payload[field] != getattr(publication, field):
            raise ValueError(f"outcome payload {field} does not match publication")
    _require_outcome_period(publication.period_start_date, publication.period_end_date)
    return validated
