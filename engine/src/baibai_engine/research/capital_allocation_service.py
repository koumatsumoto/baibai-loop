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

from .capital_allocation import (
    AllocationAlternative,
    CapitalAllocationAssessment,
    CapitalAllocationConflictError,
    capital_allocation_draft_sha256,
)
from .thesis import (
    ThesisDocument,
    ThesisError,
    ThesisReview,
    evaluate_thesis,
    require_recorded_identity,
)


@dataclass(frozen=True, slots=True)
class _StoredThesis:
    ticker: str
    core_sha256: str
    document: ThesisDocument


class CapitalAllocationAssessmentService:
    """canonical store への publish と immutable source binding の検証。"""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def check(self, assessment: CapitalAllocationAssessment) -> None:
        """store へ書かずに、参照束縛と content review binding を検証する。

        content review の hash 束縛だけは求めない。review 前の draft を検証して
        期待 hash を知るための経路であり、束縛は publish が求める。
        """
        self._verify_bindings(assessment, require_review_binding=False)

    def publish(self, assessment: CapitalAllocationAssessment) -> CapitalAllocationAssessment:
        self._verify_bindings(assessment, require_review_binding=True)
        initialize_database(self._db_path)
        payload = canonical_json(assessment.payload())
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload FROM capital_allocation_assessment "
                    "WHERE capital_allocation_assessment_id = ?",
                    (assessment.capital_allocation_assessment_id,),
                ).fetchone()
                if row is not None:
                    if str(row[0]) == payload:
                        connection.rollback()
                        return assessment
                    raise CapitalAllocationConflictError(
                        "capital allocation assessment differs from existing publication: "
                        f"{assessment.capital_allocation_assessment_id}"
                    )
                connection.execute(
                    """
                    INSERT INTO capital_allocation_assessment (
                        capital_allocation_assessment_id, as_of, published_at,
                        result, research_triage_id, payload
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        assessment.capital_allocation_assessment_id,
                        assessment.as_of.isoformat(),
                        assessment.published_at.isoformat(),
                        assessment.result,
                        assessment.research_triage_id,
                        payload,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return assessment

    def _verify_bindings(
        self, assessment: CapitalAllocationAssessment, *, require_review_binding: bool
    ) -> None:
        expected_draft = capital_allocation_draft_sha256(assessment)
        if require_review_binding and assessment.review.draft_sha256 != expected_draft:
            raise CapitalAllocationConflictError(
                f"content review binds a different draft, expected {expected_draft}"
            )
        research_triage = self._research_triage(assessment.research_triage_id)
        research_triage_as_of = str(research_triage.get("as_of", ""))
        if research_triage_as_of and assessment.as_of < date.fromisoformat(research_triage_as_of):
            raise CapitalAllocationConflictError(
                f"assessment as_of {assessment.as_of} precedes research triage "
                f"{research_triage_as_of}"
            )
        research_triage_tickers = _admissible_research_tickers(
            research_triage, assessment.research_triage_id
        )
        for alternative in assessment.alternatives:
            if alternative.ticker not in research_triage_tickers:
                raise CapitalAllocationConflictError(
                    f"alternative {alternative.ticker} is not researchable in "
                    f"{assessment.research_triage_id}"
                )
            stored = self._stored_thesis(alternative.thesis_id)
            if stored.ticker != alternative.ticker:
                raise CapitalAllocationConflictError(
                    f"thesis {alternative.thesis_id} belongs to {stored.ticker}, "
                    f"not {alternative.ticker}"
                )
            if stored.core_sha256 != alternative.thesis_core_sha256:
                raise CapitalAllocationConflictError(
                    f"thesis {alternative.thesis_id} has moved since the draft was written"
                )
            if alternative.disposition == "allocate":
                self._require_allocated_alternative_ready(assessment, alternative, stored)

    def require_allocated_alternative(
        self, capital_allocation_assessment_id: str
    ) -> AllocationAlternative:
        """Resolve the sole allocated alternative for a canonical decision."""
        row = self._row(
            "SELECT result, payload FROM capital_allocation_assessment "
            "WHERE capital_allocation_assessment_id = ?",
            (capital_allocation_assessment_id,),
        )
        if row is None:
            raise CapitalAllocationConflictError(
                f"capital allocation assessment is unavailable: {capital_allocation_assessment_id}"
            )
        try:
            assessment = CapitalAllocationAssessment.model_validate(json.loads(str(row[1])))
        except (ValueError, TypeError) as error:
            raise CapitalAllocationConflictError(
                f"assessment cannot be read: {capital_allocation_assessment_id}: {error}"
            ) from error
        if str(row[0]) != "allocate" or assessment.result != "allocate":
            raise CapitalAllocationConflictError(
                f"assessment is not an allocate decision: {capital_allocation_assessment_id}"
            )
        return next(
            alternative
            for alternative in assessment.alternatives
            if alternative.disposition == "allocate"
        )

    def _require_allocated_alternative_ready(
        self,
        assessment: CapitalAllocationAssessment,
        alternative: AllocationAlternative,
        stored: _StoredThesis,
    ) -> None:
        if stored.document.judgment.recommendation != "buy":
            raise CapitalAllocationConflictError(
                f"allocated thesis is not a buy recommendation: {alternative.thesis_id}"
            )
        assert alternative.thesis_review_id is not None
        row = self._row(
            "SELECT thesis_id, payload FROM thesis_review WHERE review_id = ?",
            (alternative.thesis_review_id,),
        )
        if row is None or str(row[0]) != alternative.thesis_id:
            raise CapitalAllocationConflictError(
                f"review {alternative.thesis_review_id} does not bind allocated thesis "
                f"{alternative.thesis_id}"
            )
        try:
            review = ThesisReview.model_validate(json.loads(str(row[1])))
        except (ValueError, TypeError) as error:
            raise CapitalAllocationConflictError(
                f"review {alternative.thesis_review_id} cannot be read: {error}"
            ) from error
        result = evaluate_thesis(
            stored.document,
            review=review,
            now=assessment.published_at,
            identity=stored.core_sha256,
        )
        if result.decision_readiness not in {"ready", "ready_with_warnings"}:
            raise CapitalAllocationConflictError(
                f"allocated thesis is not decision-ready: {alternative.thesis_id}: "
                + "; ".join(result.errors)
            )

    def _research_triage(self, research_triage_id: str) -> dict[str, object]:
        row = self._row(
            "SELECT payload FROM research_triage WHERE research_triage_id = ?",
            (research_triage_id,),
        )
        if row is None:
            raise CapitalAllocationConflictError(
                f"research triage is unavailable: {research_triage_id}"
            )
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise CapitalAllocationConflictError(
                f"research triage payload is not an object: {research_triage_id}"
            )
        return payload

    def _stored_thesis(self, thesis_id: str) -> _StoredThesis:
        row = self._row(
            "SELECT ticker, core_sha256, payload FROM thesis WHERE thesis_id = ?",
            (thesis_id,),
        )
        if row is None:
            raise CapitalAllocationConflictError(f"thesis is unavailable: {thesis_id}")
        payload = json.loads(str(row[2]))
        try:
            document = ThesisDocument.model_validate(payload)
        except (ThesisError, ValueError) as error:
            raise CapitalAllocationConflictError(
                f"thesis {thesis_id} cannot be read: {error}"
            ) from error
        return _StoredThesis(
            ticker=str(row[0]),
            core_sha256=require_recorded_identity(row[1], thesis_id),
            document=document,
        )

    def _row(self, sql: str, parameters: tuple[str, ...]) -> sqlite3.Row | None:
        path = database_path(self._db_path)
        if not path.is_file():
            raise CapitalAllocationConflictError(f"application database is unavailable: {path}")
        with closing(connect_read_only(path)) as connection:
            row: sqlite3.Row | None = connection.execute(sql, parameters).fetchone()
            return row


def _admissible_research_tickers(
    payload: Mapping[str, object], research_triage_id: str
) -> frozenset[str]:
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise CapitalAllocationConflictError(
            f"research triage has no entries: {research_triage_id}"
        )
    return frozenset(
        str(entry["ticker"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("decision") == "research"
    )


__all__ = ["CapitalAllocationAssessmentService"]
