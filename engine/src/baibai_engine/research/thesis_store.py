"""企業評価工程で Thesis と独立 Review を原子的に産み、同じ一組を見せる。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.foundation.time import JST
from baibai_engine.research.thesis import (
    ThesisDocument,
    ThesisEvaluation,
    ThesisReview,
    UnpublishedThesis,
    evaluate_thesis,
    require_recorded_identity,
    thesis_core_hash,
)


class ResearchConflictError(ValueError):
    """An immutable publication or current revision conflicts with supplied input."""


class ResearchValidationError(ValueError):
    """The supplied pair cannot be used as a Reviewed Thesis."""


@dataclass(frozen=True, slots=True)
class ReviewedThesis:
    thesis_id: str
    document: ThesisDocument
    review: ThesisReview
    core_sha256: str
    evaluation: ThesisEvaluation


def latest_thesis_id(connection: sqlite3.Connection, ticker: str) -> str | None:
    """Select the head before inspecting its schema, disposition or resolution."""
    row = connection.execute(
        "SELECT thesis_id FROM thesis WHERE ticker = ? "
        "ORDER BY as_of DESC, published_at DESC, thesis_id DESC LIMIT 1",
        (ticker,),
    ).fetchone()
    return None if row is None else str(row[0])


def load_reviewed_thesis(connection: sqlite3.Connection, thesis_id: str) -> ReviewedThesis:
    """Read one exact immutable pair without current market or capital eligibility."""
    row = connection.execute(
        "SELECT ticker, as_of, recommendation, published_at, payload, core_sha256 "
        "FROM thesis WHERE thesis_id = ?",
        (thesis_id,),
    ).fetchone()
    if row is None:
        raise ResearchValidationError(f"unknown thesis: {thesis_id}")
    try:
        document = ThesisDocument.model_validate_json(str(row[4]))
        identity = require_recorded_identity(row[5], thesis_id)
        reviews = connection.execute(
            "SELECT review_id, payload FROM thesis_review WHERE thesis_id = ?", (thesis_id,)
        ).fetchall()
        if len(reviews) != 1:
            raise ResearchValidationError("Reviewed Thesis requires exactly one Review")
        review = ThesisReview.model_validate_json(str(reviews[0][1]))
        if (
            (str(row[0]), str(row[1]), str(row[2]))
            != (
                document.input_snapshot.ticker,
                document.input_snapshot.as_of.isoformat(),
                document.judgment.disposition,
            )
            or review.review_id != str(reviews[0][0])
            or identity != thesis_core_hash(document)
        ):
            raise ResearchValidationError("Reviewed Thesis identity differs from stored content")
        evaluation = evaluate_thesis(
            document, review=review, identity=identity, now=datetime.fromisoformat(str(row[3]))
        )
        if evaluation.errors:
            raise ResearchValidationError("; ".join(evaluation.errors))
    except ValueError as error:
        raise ResearchValidationError(f"要再評価: {thesis_id}: {error}") from error
    return ReviewedThesis(thesis_id, document, review, identity, evaluation)


def load_latest_reviewed_thesis(connection: sqlite3.Connection, ticker: str) -> ReviewedThesis:
    thesis_id = latest_thesis_id(connection, ticker)
    if thesis_id is None:
        raise ResearchValidationError(f"no thesis for {ticker}")
    return load_reviewed_thesis(connection, thesis_id)


def publish_reviewed_thesis(
    connection: sqlite3.Connection,
    thesis_id: str,
    thesis_payload: Mapping[str, object],
    review_payload: Mapping[str, object],
    *,
    published_at: datetime,
    supersedes_id: str | None = None,
) -> ReviewedThesis:
    """Validate/insert inside the caller's transaction; retry compares both payloads first."""
    if not connection.in_transaction:
        raise ResearchValidationError("publication requires an existing transaction")
    if not thesis_id.strip() or published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ResearchValidationError("publication requires an ID and timezone-aware time")
    thesis_json = canonical_json(thesis_payload)
    review_json = canonical_json(review_payload)
    existing = connection.execute(
        "SELECT payload, supersedes_id FROM thesis WHERE thesis_id = ?", (thesis_id,)
    ).fetchone()
    if existing is not None:
        reviews = connection.execute(
            "SELECT payload FROM thesis_review WHERE thesis_id = ?", (thesis_id,)
        ).fetchall()
        if (
            str(existing[0]) != thesis_json
            or existing[1] != supersedes_id
            or len(reviews) != 1
            or str(reviews[0][0]) != review_json
        ):
            raise ResearchConflictError(f"immutable Reviewed Thesis differs: {thesis_id}")
        return load_reviewed_thesis(connection, thesis_id)
    thesis = ThesisDocument.model_validate(thesis_payload)
    review = ThesisReview.model_validate(review_payload)
    result = evaluate_thesis(
        thesis, review=review, identity=UnpublishedThesis.DRAFT, now=published_at
    )
    if result.errors:
        raise ResearchValidationError("; ".join(result.errors))
    latest = latest_thesis_id(connection, thesis.input_snapshot.ticker)
    if latest != supersedes_id:
        raise ResearchConflictError(f"new revision must supersede latest thesis: {latest}")
    if latest is not None:
        prior = connection.execute(
            "SELECT as_of, published_at FROM thesis WHERE thesis_id = ?", (latest,)
        ).fetchone()
        if thesis.input_snapshot.as_of.isoformat() < str(prior[0]):
            raise ResearchConflictError("new thesis cannot move as_of backwards")
        if (thesis.input_snapshot.as_of.isoformat(), published_at.isoformat(), thesis_id) <= (
            str(prior[0]),
            str(prior[1]),
            latest,
        ):
            raise ResearchConflictError("new thesis must advance the latest revision order")
    connection.execute("SAVEPOINT reviewed_thesis_publication")
    try:
        connection.execute(
            "INSERT INTO thesis(thesis_id,ticker,as_of,recommendation,published_at,"
            "supersedes_id,payload,core_sha256) VALUES (?,?,?,?,?,?,?,?)",
            (
                thesis_id,
                thesis.input_snapshot.ticker,
                thesis.input_snapshot.as_of.isoformat(),
                thesis.judgment.disposition,
                published_at.isoformat(),
                supersedes_id,
                thesis_json,
                result.thesis_sha256,
            ),
        )
        connection.execute(
            "INSERT INTO thesis_review(review_id,thesis_id,reviewed_at,payload) VALUES (?,?,?,?)",
            (review.review_id, thesis_id, review.reviewed_at.isoformat(), review_json),
        )
    except BaseException:
        connection.execute("ROLLBACK TO reviewed_thesis_publication")
        connection.execute("RELEASE reviewed_thesis_publication")
        raise
    connection.execute("RELEASE reviewed_thesis_publication")
    return ReviewedThesis(thesis_id, thesis, review, result.thesis_sha256, result)


class ThesisStoreService:
    """Own the short write transaction for a new immutable pair."""

    def __init__(
        self, db_path: Path | None = None, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._db_path = db_path
        self._clock = clock or (lambda: datetime.now(JST))

    def publish_reviewed_thesis(
        self,
        thesis_id: str,
        thesis_payload: Mapping[str, object],
        review_payload: Mapping[str, object],
        *,
        supersedes_id: str | None = None,
    ) -> ReviewedThesis:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                pair = publish_reviewed_thesis(
                    connection,
                    thesis_id,
                    thesis_payload,
                    review_payload,
                    published_at=self._clock(),
                    supersedes_id=supersedes_id,
                )
                connection.commit()
                return pair
            except BaseException:
                connection.rollback()
                raise
