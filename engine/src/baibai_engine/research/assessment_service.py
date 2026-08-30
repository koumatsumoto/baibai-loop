"""Assessment publication service — verified judgment rowsを産む write boundary。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.appdb.write import connect_rw, initialize_database

from .assessment import (
    AssessmentCase,
    AssessmentConflictError,
    BargainAssessment,
    assessment_draft_sha256,
)
from .thesis import (
    IndependentReview,
    ThesisDocument,
    ThesisError,
    evaluate_thesis,
    require_recorded_identity,
)


@dataclass(frozen=True, slots=True)
class _StoredThesis:
    ticker: str
    core_sha256: str
    document: ThesisDocument


class BargainAssessmentService:
    """canonical store への publish と immutable source binding の検証。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def check(self, assessment: BargainAssessment) -> None:
        """store へ書かずに、参照束縛と content review binding を検証する。

        content review の hash 束縛だけは求めない。review 前の draft を検証して
        期待 hash を知るための経路であり、束縛は publish が求める。
        """
        self._verify_bindings(assessment, require_review_binding=False)

    def publish(self, assessment: BargainAssessment) -> BargainAssessment:
        self._verify_bindings(assessment, require_review_binding=True)
        initialize_database(self._db_path)
        payload = canonical_json(assessment.payload())
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload FROM bargain_assessment WHERE assessment_id = ?",
                    (assessment.assessment_id,),
                ).fetchone()
                if row is not None:
                    if str(row[0]) == payload:
                        connection.rollback()
                        return assessment
                    raise AssessmentConflictError(
                        f"assessment differs from existing publication: {assessment.assessment_id}"
                    )
                connection.execute(
                    """
                    INSERT INTO bargain_assessment (
                        assessment_id, as_of, published_at, result, shortlist_id, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assessment.assessment_id,
                        assessment.as_of.isoformat(),
                        assessment.published_at.isoformat(),
                        assessment.result,
                        assessment.shortlist_id,
                        payload,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return assessment

    def _verify_bindings(
        self, assessment: BargainAssessment, *, require_review_binding: bool
    ) -> None:
        expected_draft = assessment_draft_sha256(assessment)
        if require_review_binding and assessment.review.draft_sha256 != expected_draft:
            raise AssessmentConflictError(
                f"content review binds a different draft, expected {expected_draft}"
            )
        shortlist = self._shortlist(assessment.shortlist_id)
        shortlist_as_of = str(shortlist.get("as_of", ""))
        if shortlist_as_of and assessment.as_of < date.fromisoformat(shortlist_as_of):
            raise AssessmentConflictError(
                f"assessment as_of {assessment.as_of} precedes shortlist {shortlist_as_of}"
            )
        shortlist_tickers = _selected_tickers(shortlist, assessment.shortlist_id)
        for case in assessment.cases:
            if case.ticker not in shortlist_tickers:
                raise AssessmentConflictError(
                    f"case {case.ticker} is not a selected candidate of {assessment.shortlist_id}"
                )
            stored = self._stored_thesis(case.thesis_id)
            if stored.ticker != case.ticker:
                raise AssessmentConflictError(
                    f"thesis {case.thesis_id} belongs to {stored.ticker}, not {case.ticker}"
                )
            if stored.core_sha256 != case.thesis_core_sha256:
                raise AssessmentConflictError(
                    f"thesis {case.thesis_id} has moved since the draft was written"
                )
            if case.disposition == "selected":
                self._require_buy_case_ready(assessment, case, stored)

    def require_buy_case(self, assessment_id: str) -> AssessmentCase:
        """Resolve the sole selected case for a canonical buy decision."""
        row = self._row(
            "SELECT result, payload FROM bargain_assessment WHERE assessment_id = ?",
            (assessment_id,),
        )
        if row is None:
            raise AssessmentConflictError(f"assessment is unavailable: {assessment_id}")
        try:
            assessment = BargainAssessment.model_validate(json.loads(str(row[1])))
        except (ValueError, TypeError) as error:
            raise AssessmentConflictError(
                f"assessment cannot be read: {assessment_id}: {error}"
            ) from error
        if str(row[0]) != "buy" or assessment.result != "buy":
            raise AssessmentConflictError(f"assessment is not a buy decision: {assessment_id}")
        return next(case for case in assessment.cases if case.disposition == "selected")

    def _require_buy_case_ready(
        self,
        assessment: BargainAssessment,
        case: AssessmentCase,
        stored: _StoredThesis,
    ) -> None:
        if stored.document.judgment.recommendation != "buy":
            raise AssessmentConflictError(
                f"selected thesis is not a buy recommendation: {case.thesis_id}"
            )
        assert case.review_id is not None
        row = self._row(
            "SELECT thesis_id, payload FROM thesis_review WHERE review_id = ?",
            (case.review_id,),
        )
        if row is None or str(row[0]) != case.thesis_id:
            raise AssessmentConflictError(
                f"review {case.review_id} does not bind selected thesis {case.thesis_id}"
            )
        try:
            review = IndependentReview.model_validate(json.loads(str(row[1])))
        except (ValueError, TypeError) as error:
            raise AssessmentConflictError(
                f"review {case.review_id} cannot be read: {error}"
            ) from error
        result = evaluate_thesis(
            stored.document,
            review=review,
            now=assessment.published_at,
            identity=stored.core_sha256,
        )
        if result.decision_readiness not in {"ready", "ready_with_warnings"}:
            raise AssessmentConflictError(
                f"selected thesis is not decision-ready: {case.thesis_id}: "
                + "; ".join(result.errors)
            )

    def _shortlist(self, shortlist_id: str) -> dict[str, object]:
        row = self._row(
            "SELECT payload FROM shortlist WHERE shortlist_id = ?",
            (shortlist_id,),
        )
        if row is None:
            raise AssessmentConflictError(f"shortlist is unavailable: {shortlist_id}")
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise AssessmentConflictError(f"shortlist payload is not an object: {shortlist_id}")
        return payload

    def _stored_thesis(self, thesis_id: str) -> _StoredThesis:
        row = self._row(
            "SELECT ticker, core_sha256, payload FROM thesis WHERE thesis_id = ?",
            (thesis_id,),
        )
        if row is None:
            raise AssessmentConflictError(f"thesis is unavailable: {thesis_id}")
        payload = json.loads(str(row[2]))
        try:
            document = ThesisDocument.model_validate(payload)
        except (ThesisError, ValueError) as error:
            raise AssessmentConflictError(f"thesis {thesis_id} cannot be read: {error}") from error
        return _StoredThesis(
            ticker=str(row[0]),
            core_sha256=require_recorded_identity(row[1], thesis_id),
            document=document,
        )

    def _row(self, sql: str, parameters: tuple[str, ...]) -> sqlite3.Row | None:
        path = database_path(self._db_path)
        if not path.is_file():
            raise AssessmentConflictError(f"application database is unavailable: {path}")
        with closing(connect_read_only(path)) as connection:
            row: sqlite3.Row | None = connection.execute(sql, parameters).fetchone()
            return row


def _selected_tickers(payload: Mapping[str, object], shortlist_id: str) -> frozenset[str]:
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise AssessmentConflictError(f"shortlist has no entries: {shortlist_id}")
    return frozenset(
        str(entry["ticker"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("decision") == "selected"
    )


__all__ = ["BargainAssessmentService"]
