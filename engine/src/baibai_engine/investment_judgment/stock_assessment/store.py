"""銘柄評価と今回の独立 Review を原子的に保存し、同じ一組を再読する。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import TypeAdapter

from baibai_engine.investment_judgment.contracts import (
    AssessmentId,
)
from baibai_engine.investment_judgment.independent_review.contracts import (
    AssessmentReview,
)
from baibai_engine.investment_judgment.stock_assessment.document import (
    AssessmentEvaluation,
    AssessmentRef,
    StockAssessmentDocument,
    UnpublishedAssessment,
    assessment_core_hash,
    evaluate_assessment,
    require_recorded_identity,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.write import connect_rw, initialize_database


class ResearchConflictError(ValueError):
    """An immutable publication or current revision conflicts with supplied input."""


class ResearchValidationError(ValueError):
    """The supplied pair cannot be used as a Reviewed Assessment."""


@dataclass(frozen=True, slots=True)
class ReviewedAssessment:
    assessment_id: str
    document: StockAssessmentDocument
    review: AssessmentReview
    core_sha256: str
    evaluation: AssessmentEvaluation


def latest_assessment_id(connection: sqlite3.Connection, ticker: str) -> str | None:
    """Select the head before inspecting its schema, stance or resolution."""
    rows = connection.execute(
        "SELECT assessment_id, published_at FROM stock_assessment WHERE ticker = ? "
        "AND as_of = (SELECT MAX(as_of) FROM stock_assessment WHERE ticker = ?)",
        (ticker, ticker),
    ).fetchall()
    headers: list[tuple[datetime, str]] = []
    for row in rows:
        try:
            instant = datetime.fromisoformat(str(row[1]))
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError("stored publication time must include a timezone")
        except ValueError as error:
            raise ResearchValidationError(
                f"invalid Assessment revision header: {row[0]}"
            ) from error
        headers.append((instant, str(row[0])))
    # SQLite julianday loses sub-millisecond ordering; use the writer's exact
    # aware-datetime comparison without changing any recorded timestamps.
    return max(headers)[1] if headers else None


def load_reviewed_assessment(
    connection: sqlite3.Connection, assessment_id: str
) -> ReviewedAssessment:
    """Read one exact immutable pair without current market or capital eligibility."""
    row = connection.execute(
        "SELECT ticker, as_of, stance, published_at, payload, core_sha256 "
        "FROM stock_assessment WHERE assessment_id = ?",
        (assessment_id,),
    ).fetchone()
    if row is None:
        raise ResearchValidationError(f"unknown assessment: {assessment_id}")
    try:
        TypeAdapter(AssessmentId).validate_python(assessment_id)
        document = StockAssessmentDocument.model_validate_json(str(row[4]))
        identity = require_recorded_identity(row[5], assessment_id)
        reviews = connection.execute(
            "SELECT review_id, payload, reviewed_at FROM stock_assessment_review "
            "WHERE assessment_id = ?",
            (assessment_id,),
        ).fetchall()
        if len(reviews) != 1:
            raise ResearchValidationError("Reviewed Assessment requires exactly one Review")
        review = AssessmentReview.model_validate_json(str(reviews[0][1]))
        published_at = datetime.fromisoformat(str(row[3]))
        if published_at.tzinfo is None or published_at.utcoffset() is None:
            raise ResearchValidationError("stored publication time must include a timezone")
        if (
            (str(row[0]), str(row[1]), str(row[2]))
            != (
                document.input_snapshot.ticker,
                document.input_snapshot.as_of.isoformat(),
                document.conclusion.stance,
            )
            or review.review_id != str(reviews[0][0])
            or review.reviewed_at != datetime.fromisoformat(str(reviews[0][2]))
            or identity != assessment_core_hash(document)
        ):
            raise ResearchValidationError(
                "Reviewed Assessment identity differs from stored content"
            )
        evaluation = evaluate_assessment(
            document, review=review, identity=identity, now=published_at
        )
        if evaluation.errors:
            raise ResearchValidationError("; ".join(evaluation.errors))
        if document.refresh is not None:
            prior_ref = document.refresh.prior_assessment_ref
            if prior_ref.assessment_id == assessment_id:
                raise ResearchValidationError("Assessment cannot refresh itself")
            _verify_prior_ref(connection, prior_ref, document)
    except ValueError as error:
        raise ResearchValidationError(f"要再評価: {assessment_id}: {error}") from error
    return ReviewedAssessment(assessment_id, document, review, identity, evaluation)


def load_latest_reviewed_assessment(
    connection: sqlite3.Connection, ticker: str
) -> ReviewedAssessment:
    assessment_id = latest_assessment_id(connection, ticker)
    if assessment_id is None:
        raise ResearchValidationError(f"no assessment for {ticker}")
    return load_reviewed_assessment(connection, assessment_id)


def require_assessment_ref(
    connection: sqlite3.Connection, reference: AssessmentRef
) -> ReviewedAssessment:
    """Resolve an exact immutable publication and its independent Review."""
    pair = load_reviewed_assessment(connection, reference.assessment_id)
    if pair.core_sha256 != reference.core_sha256 or pair.review.review_id != reference.review_id:
        raise ResearchValidationError("Assessment reference differs from stored pair")
    return pair


def _verify_prior_ref(
    connection: sqlite3.Connection,
    reference: AssessmentRef,
    document: StockAssessmentDocument,
) -> None:
    # Verify the immediate stored identity without recursively traversing all revisions.
    row = connection.execute(
        "SELECT a.ticker, a.as_of, a.core_sha256, r.review_id, a.payload, r.payload, "
        "a.published_at, r.reviewed_at, a.stance "
        "FROM stock_assessment a JOIN stock_assessment_review r USING (assessment_id) "
        "WHERE a.assessment_id = ?",
        (reference.assessment_id,),
    ).fetchone()
    if row is None or (str(row[2]), str(row[3])) != (reference.core_sha256, reference.review_id):
        raise ResearchValidationError("refresh prior Assessment reference differs from stored pair")
    prior_document = StockAssessmentDocument.model_validate_json(str(row[4]))
    prior_review = AssessmentReview.model_validate_json(str(row[5]))
    if (
        str(row[0]) != document.input_snapshot.ticker
        or str(row[0]) != prior_document.input_snapshot.ticker
        or str(row[1]) != prior_document.input_snapshot.as_of.isoformat()
        or str(row[1]) > document.input_snapshot.as_of.isoformat()
        or assessment_core_hash(prior_document) != reference.core_sha256
        or prior_review.review_id != reference.review_id
        or prior_review.reviewed_assessment_sha256 != reference.core_sha256
        or prior_review.reviewed_at != datetime.fromisoformat(str(row[7]))
        or str(row[8]) != prior_document.conclusion.stance
    ):
        raise ResearchValidationError("refresh prior Assessment identity mismatch")
    prior_evaluation = evaluate_assessment(
        prior_document,
        review=prior_review,
        identity=reference.core_sha256,
        now=datetime.fromisoformat(str(row[6])),
    )
    if prior_evaluation.errors:
        raise ResearchValidationError("; ".join(prior_evaluation.errors))
    refresh = document.refresh
    assert refresh is not None
    if document.conclusion.assessed_at < prior_document.conclusion.assessed_at:
        raise ResearchValidationError("refresh assessment time cannot precede the prior assessment")
    if refresh.checked_at < datetime.fromisoformat(str(row[6])):
        raise ResearchValidationError("refresh check cannot precede the prior publication")


def publish_reviewed_assessment(
    connection: sqlite3.Connection,
    assessment_id: str,
    assessment_payload: Mapping[str, object],
    review_payload: Mapping[str, object],
    *,
    published_at: datetime,
    supersedes_id: str | None = None,
) -> ReviewedAssessment:
    """Validate/insert inside the caller's transaction; retry compares both payloads first."""
    if not connection.in_transaction:
        raise ResearchValidationError("publication requires an existing transaction")
    if not assessment_id.strip() or published_at.tzinfo is None or published_at.utcoffset() is None:
        raise ResearchValidationError("publication requires an ID and timezone-aware time")
    TypeAdapter(AssessmentId).validate_python(assessment_id)
    assessment = StockAssessmentDocument.model_validate(assessment_payload)
    review = AssessmentReview.model_validate(review_payload)
    assessment_json = canonical_json(assessment.model_dump(mode="json"))
    review_json = canonical_json(review.model_dump(mode="json"))
    existing = connection.execute(
        "SELECT payload, supersedes_id FROM stock_assessment WHERE assessment_id = ?",
        (assessment_id,),
    ).fetchone()
    if existing is not None:
        reviews = connection.execute(
            "SELECT payload FROM stock_assessment_review WHERE assessment_id = ?", (assessment_id,)
        ).fetchall()
        if (
            str(existing[0]) != assessment_json
            or existing[1] != supersedes_id
            or len(reviews) != 1
            or canonical_json(
                AssessmentReview.model_validate_json(str(reviews[0][0])).model_dump(mode="json")
            )
            != review_json
        ):
            raise ResearchConflictError(f"immutable Reviewed Assessment differs: {assessment_id}")
        return load_reviewed_assessment(connection, assessment_id)
    result = evaluate_assessment(
        assessment, review=review, identity=UnpublishedAssessment.DRAFT, now=published_at
    )
    if result.errors:
        raise ResearchValidationError("; ".join(result.errors))
    latest = latest_assessment_id(connection, assessment.input_snapshot.ticker)
    if latest != supersedes_id:
        raise ResearchConflictError(f"new revision must supersede latest assessment: {latest}")
    if latest is not None:
        prior = connection.execute(
            "SELECT as_of, published_at FROM stock_assessment WHERE assessment_id = ?", (latest,)
        ).fetchone()
        if assessment.input_snapshot.as_of.isoformat() < str(prior[0]):
            raise ResearchConflictError("new assessment cannot move as_of backwards")
        prior_published_at = datetime.fromisoformat(str(prior[1]))
        if published_at < prior_published_at:
            raise ResearchConflictError("new assessment cannot move publication time backwards")
        if (assessment.input_snapshot.as_of, published_at, assessment_id) <= (
            datetime.fromisoformat(str(prior[0])).date(),
            prior_published_at,
            latest,
        ):
            raise ResearchConflictError("new assessment must advance the latest revision order")
    if assessment.refresh is not None:
        _verify_prior_ref(connection, assessment.refresh.prior_assessment_ref, assessment)
    connection.execute("SAVEPOINT reviewed_assessment_publication")
    try:
        connection.execute(
            "INSERT INTO stock_assessment(assessment_id,ticker,as_of,stance,published_at,"
            "supersedes_id,payload,core_sha256) VALUES (?,?,?,?,?,?,?,?)",
            (
                assessment_id,
                assessment.input_snapshot.ticker,
                assessment.input_snapshot.as_of.isoformat(),
                assessment.conclusion.stance,
                published_at.isoformat(),
                supersedes_id,
                assessment_json,
                result.assessment_sha256,
            ),
        )
        connection.execute(
            "INSERT INTO stock_assessment_review(review_id,assessment_id,reviewed_at,payload) "
            "VALUES (?,?,?,?)",
            (review.review_id, assessment_id, review.reviewed_at.isoformat(), review_json),
        )
    except BaseException:
        connection.execute("ROLLBACK TO reviewed_assessment_publication")
        connection.execute("RELEASE reviewed_assessment_publication")
        raise
    connection.execute("RELEASE reviewed_assessment_publication")
    return ReviewedAssessment(assessment_id, assessment, review, result.assessment_sha256, result)


class AssessmentStoreService:
    """Own the short write transaction for a new immutable pair."""

    def __init__(
        self, db_path: Path | None = None, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._db_path = db_path
        self._clock = clock or (lambda: datetime.now(JST))

    def publish_reviewed_assessment(
        self,
        assessment_id: str,
        assessment_payload: Mapping[str, object],
        review_payload: Mapping[str, object],
        *,
        supersedes_id: str | None = None,
    ) -> ReviewedAssessment:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                pair = publish_reviewed_assessment(
                    connection,
                    assessment_id,
                    assessment_payload,
                    review_payload,
                    published_at=self._clock(),
                    supersedes_id=supersedes_id,
                )
                connection.commit()
                return pair
            except BaseException:
                connection.rollback()
                raise
