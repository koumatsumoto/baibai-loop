"""Immutable application-DB publications for portfolio outcomes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database


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


class PortfolioOutcomeStore:
    """Publish and query immutable outcome records."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def publish(self, publication: PortfolioOutcomePublication) -> bool:
        _validate(publication)
        encoded = canonical_json(publication.payload)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
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
                    return False
                connection.execute(
                    "INSERT INTO portfolio_outcome "
                    "(outcome_id, horizon, period_start_date, period_end_date, status, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (publication.outcome_id, *expected),
                )
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def list(self) -> tuple[Mapping[str, object], ...]:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            rows = connection.execute(
                "SELECT outcome_id, payload FROM portfolio_outcome "
                "ORDER BY period_end_date DESC, outcome_id DESC"
            ).fetchall()
        return tuple(
            {"outcome_id": str(row["outcome_id"]), **json.loads(str(row["payload"]))}
            for row in rows
        )


def _validate(publication: PortfolioOutcomePublication) -> None:
    payload = publication.payload
    required = {
        "kind": "portfolio_outcome",
        "horizon": publication.horizon,
        "period_start_date": publication.period_start_date,
        "period_end_date": publication.period_end_date,
        "status": publication.status,
    }
    for field, expected in required.items():
        if payload.get(field) != expected:
            raise ValueError(f"outcome payload {field} does not match publication")
    if not isinstance(payload.get("benchmark_observation"), Mapping):
        raise ValueError("outcome payload must embed benchmark_observation")
