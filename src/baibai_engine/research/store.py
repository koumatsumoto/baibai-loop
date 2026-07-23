"""Immutable application-DB storage for research publications."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.position.holding_review import (
    HoldingReviewDocument,
    evaluate_holding_review,
)
from baibai_engine.research.holding_review_builder import validate_holding_review_scalars_from_db
from baibai_engine.research.thesis import (
    IndependentReview,
    ThesisDocument,
    ThesisResult,
    evaluate_thesis,
)

_REVIEW_REQUIRED = "buy recommendation requires an independent second-pass review"


class ResearchConflictError(ValueError):
    """A publication conflicts with an immutable row or source revision."""


class ResearchValidationError(ValueError):
    """A publication does not satisfy the research domain contract."""


@dataclass(frozen=True, slots=True)
class ThesisPublication:
    thesis_id: str
    payload: Mapping[str, object]
    supersedes_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewPublication:
    thesis_id: str
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class HoldingReviewPublication:
    holding_review_id: str
    thesis_id: str
    payload: Mapping[str, object]
    candidate_thesis_id: str | None = None


class ResearchStoreService:
    """Validate revision bindings before writing immutable research rows."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def publish_thesis(
        self,
        thesis_id: str,
        payload: Mapping[str, object],
        *,
        supersedes_id: str | None = None,
    ) -> ThesisDocument:
        publication = ThesisPublication(thesis_id, payload, supersedes_id)
        thesis, _ = _validate_thesis(publication)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _insert_thesis(connection, publication, thesis)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return thesis

    def publish_thesis_with_review(
        self,
        thesis_id: str,
        thesis_payload: Mapping[str, object],
        review_payload: Mapping[str, object],
        *,
        supersedes_id: str | None = None,
    ) -> tuple[ThesisDocument, IndependentReview]:
        """Atomically publish a thesis and its independent review."""
        publication = ThesisPublication(thesis_id, thesis_payload, supersedes_id)
        thesis, _ = _validate_thesis(publication, allow_review_required=True)
        review = IndependentReview.model_validate(review_payload)
        _require_valid(evaluate_thesis(thesis, review=review))
        review_publication = ReviewPublication(thesis_id, review_payload)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _insert_thesis(connection, publication, thesis)
                _insert_review(connection, review_publication, review)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return thesis, review

    def publish_review(
        self,
        thesis_id: str,
        payload: Mapping[str, object],
    ) -> IndependentReview:
        publication = ReviewPublication(thesis_id, payload)
        review = IndependentReview.model_validate(payload)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _insert_review(connection, publication, review)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return review

    def publish_holding_review(
        self,
        holding_review_id: str,
        thesis_id: str,
        payload: Mapping[str, object],
        *,
        candidate_thesis_id: str | None = None,
    ) -> HoldingReviewDocument:
        """Recheck canonical DB revision bindings inside the write transaction."""
        publication = HoldingReviewPublication(
            holding_review_id, thesis_id, payload, candidate_thesis_id
        )
        document = _validate_holding_document(payload)
        validate_holding_review_scalars_from_db(document, db_path=self._db_path)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                _validate_canonical_holding_sources(connection, publication, document)
                _insert_holding_review(connection, publication, document)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return document


def _validate_thesis(
    publication: ThesisPublication,
    *,
    allow_review_required: bool = False,
) -> tuple[ThesisDocument, ThesisResult]:
    if not publication.thesis_id.strip():
        raise ResearchValidationError("thesis_id must not be empty")
    thesis = ThesisDocument.model_validate(publication.payload)
    result = evaluate_thesis(thesis)
    if result.errors and not (allow_review_required and result.errors == (_REVIEW_REQUIRED,)):
        _require_valid(result)
    return thesis, result


def _require_valid(result: ThesisResult) -> None:
    if result.errors:
        raise ResearchValidationError("; ".join(result.errors))


def _validate_holding_document(payload: Mapping[str, object]) -> HoldingReviewDocument:
    document = HoldingReviewDocument.model_validate(payload)
    result = evaluate_holding_review(document)
    if result.errors:
        raise ResearchValidationError("; ".join(result.errors))
    return document


def _insert_thesis(
    connection: sqlite3.Connection,
    publication: ThesisPublication,
    document: ThesisDocument,
) -> bool:
    payload = canonical_json(publication.payload)
    expected = (
        document.input_snapshot.ticker,
        document.input_snapshot.as_of.isoformat(),
        document.judgment.recommendation,
        document.judgment.proposed_at.isoformat(),
        publication.supersedes_id,
        payload,
    )
    existing = connection.execute(
        """
        SELECT ticker, as_of, recommendation, published_at, supersedes_id, payload
        FROM thesis WHERE thesis_id = ?
        """,
        (publication.thesis_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) == expected:
            return False
        raise ResearchConflictError(
            f"thesis differs from existing immutable revision: {publication.thesis_id}"
        )
    if publication.supersedes_id is not None:
        parent = connection.execute(
            "SELECT ticker FROM thesis WHERE thesis_id = ?",
            (publication.supersedes_id,),
        ).fetchone()
        if parent is None:
            raise ResearchConflictError(f"unknown supersedes thesis: {publication.supersedes_id}")
        if str(parent[0]) != document.input_snapshot.ticker:
            raise ResearchConflictError("supersedes thesis ticker does not match")
    connection.execute(
        """
        INSERT INTO thesis (
            thesis_id, ticker, as_of, recommendation, published_at, supersedes_id, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (publication.thesis_id, *expected),
    )
    return True


def _thesis_row(connection: sqlite3.Connection, thesis_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT ticker, as_of, payload FROM thesis WHERE thesis_id = ?",
        (thesis_id,),
    ).fetchone()
    if row is None:
        raise ResearchConflictError(f"unknown thesis revision: {thesis_id}")
    return cast(sqlite3.Row, row)


def _insert_review(
    connection: sqlite3.Connection,
    publication: ReviewPublication,
    review: IndependentReview,
) -> bool:
    thesis_row = _thesis_row(connection, publication.thesis_id)
    thesis = ThesisDocument.model_validate_json(str(thesis_row["payload"]))
    _require_valid(evaluate_thesis(thesis, review=review))
    payload = canonical_json(publication.payload)
    expected = (publication.thesis_id, review.reviewed_at.isoformat(), payload)
    existing = connection.execute(
        "SELECT thesis_id, reviewed_at, payload FROM thesis_review WHERE review_id = ?",
        (review.review_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) == expected:
            return False
        raise ResearchConflictError(
            f"review differs from existing immutable publication: {review.review_id}"
        )
    connection.execute(
        "INSERT INTO thesis_review(review_id, thesis_id, reviewed_at, payload) VALUES (?, ?, ?, ?)",
        (review.review_id, *expected),
    )
    return True


def _insert_holding_review(
    connection: sqlite3.Connection,
    publication: HoldingReviewPublication,
    document: HoldingReviewDocument,
) -> bool:
    thesis = _thesis_row(connection, publication.thesis_id)
    if document.ticker != str(thesis["ticker"]):
        raise ResearchConflictError("holding review ticker does not match thesis revision")
    if document.as_of < date.fromisoformat(str(thesis["as_of"])):
        raise ResearchConflictError("holding review predates thesis revision")
    candidate_source = document.sources.candidate_thesis
    if (candidate_source is None) != (publication.candidate_thesis_id is None):
        raise ResearchConflictError(
            "candidate_thesis_id is required exactly when candidate_thesis source exists"
        )
    if publication.candidate_thesis_id is not None:
        candidate_thesis = _thesis_row(connection, publication.candidate_thesis_id)
        candidate = document.replacement_comparison.candidate
        if candidate is None or candidate.ticker != str(candidate_thesis["ticker"]):
            raise ResearchConflictError(
                "holding review candidate ticker does not match candidate thesis revision"
            )
    payload = canonical_json(publication.payload)
    expected = (
        document.ticker,
        document.as_of.isoformat(),
        publication.thesis_id,
        publication.candidate_thesis_id,
        payload,
    )
    existing = connection.execute(
        """
        SELECT ticker, as_of, thesis_id, candidate_thesis_id, payload
        FROM holding_review WHERE holding_review_id = ?
        """,
        (publication.holding_review_id,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) == expected:
            return False
        raise ResearchConflictError(
            "holding review differs from existing immutable publication: "
            f"{publication.holding_review_id}"
        )
    connection.execute(
        """
        INSERT INTO holding_review (
            holding_review_id, ticker, as_of, thesis_id, candidate_thesis_id, payload
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (publication.holding_review_id, *expected),
    )
    return True


def _validate_canonical_holding_sources(
    connection: sqlite3.Connection,
    publication: HoldingReviewPublication,
    document: HoldingReviewDocument,
) -> None:
    ledger_source = document.sources.ledger
    thesis_source = document.sources.holding_thesis
    candidate_source = document.sources.candidate_thesis
    current_head = int(
        connection.execute("SELECT coalesce(max(append_seq), 0) FROM ledger_event").fetchone()[0]
    )
    if ledger_source.entity_id != "portfolio-ledger" or ledger_source.append_head != current_head:
        raise ResearchConflictError("holding review ledger revision changed")
    if thesis_source.entity_id != publication.thesis_id:
        raise ResearchConflictError("holding review thesis revision binding differs")
    if candidate_source is None:
        if publication.candidate_thesis_id is not None:
            raise ResearchConflictError("candidate thesis revision binding is missing")
    elif candidate_source.entity_id != publication.candidate_thesis_id:
        raise ResearchConflictError("candidate thesis revision binding differs")


__all__ = [
    "HoldingReviewPublication",
    "ResearchConflictError",
    "ResearchStoreService",
    "ResearchValidationError",
    "ReviewPublication",
    "ThesisPublication",
]
