"""Assessment publication service — verified judgment rowsを産む write boundary。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.foundation.repository_layout import MARKET_DB_PATH
from baibai_engine.foundation.time import JST
from baibai_engine.research.operation.research_binding import require_active_research_set
from baibai_engine.storage.application.json import canonical_json
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only
from baibai_engine.storage.application.write import connect_rw, initialize_database

from .capital_allocation import (
    CapitalAllocationAssessment,
    CapitalAllocationConflictError,
    capital_allocation_draft_sha256,
)
from .capital_inputs import evaluate_allocation_in_transaction
from .thesis_store import load_reviewed_thesis


class CapitalAllocationAssessmentService:
    """canonical store への publish と immutable source binding の検証。"""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        sqlite_path: Path = MARKET_DB_PATH,
    ) -> None:
        self._db_path = db_path
        self._sqlite_path = sqlite_path
        self._clock = clock or (lambda: datetime.now(JST))

    def _operation_now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise CapitalAllocationConflictError("operation clock must include a timezone")
        return now

    def check(self, assessment: CapitalAllocationAssessment) -> None:
        """store へ書かずに、参照束縛と content review binding を検証する。

        content review の hash 束縛だけは求めない。review 前の draft を検証して
        期待 hash を知るための経路であり、束縛は publish が求める。
        """
        now = self._operation_now()
        with closing(connect_read_only(self._existing_path())) as connection:
            connection.execute("BEGIN")
            self._verify_bindings(connection, assessment, now=now, require_review_binding=False)

    def publish(self, assessment: CapitalAllocationAssessment) -> CapitalAllocationAssessment:
        if not isinstance(assessment, CapitalAllocationAssessment):
            raise CapitalAllocationConflictError("stored allocation projections are read-only")
        now = self._operation_now()
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT payload FROM capital_allocation_assessment "
                    "WHERE capital_allocation_assessment_id = ?",
                    (assessment.capital_allocation_assessment_id,),
                ).fetchone()
                if row is not None:
                    stored_payload = json.loads(str(row[0]))
                    if stored_payload.get("schema_version") != 2:
                        raise CapitalAllocationConflictError(
                            "historical publication cannot be reissued"
                        )
                    stored = CapitalAllocationAssessment.model_validate(stored_payload)
                    if stored.model_dump(exclude={"published_at"}) == assessment.model_dump(
                        exclude={"published_at"}
                    ):
                        connection.rollback()
                        return stored
                    raise CapitalAllocationConflictError(
                        "capital allocation assessment differs from existing publication: "
                        f"{assessment.capital_allocation_assessment_id}"
                    )
                self._verify_bindings(connection, assessment, now=now, require_review_binding=True)
                assessment = assessment.model_copy(update={"published_at": now})
                payload = canonical_json(assessment.payload())
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
        self,
        connection: sqlite3.Connection,
        assessment: CapitalAllocationAssessment,
        *,
        now: datetime,
        require_review_binding: bool,
    ) -> None:
        if assessment.as_of != now.astimezone(JST).date():
            raise CapitalAllocationConflictError("formal assessment basis must be today")
        expected_draft = capital_allocation_draft_sha256(assessment)
        if require_review_binding and assessment.review.draft_sha256 != expected_draft:
            raise CapitalAllocationConflictError(
                f"content review binds a different draft, expected {expected_draft}"
            )
        research_triage = (
            {}
            if assessment.research_triage_id is None
            else self._research_triage(connection, assessment.research_triage_id)
        )
        # Validate the authored draft before replacing its timestamp, so a draft
        # from before this Operation cannot be made current by the writer's clock.
        self._verify_research_set(
            connection,
            assessment.model_copy(update={"published_at": min(assessment.published_at, now)}),
        )
        # NULLはTriageを偽造しないための表現。対象資格を無検証にする意味ではない。
        # 上で固定Operationの完成Thesis/Reviewと未完了被覆へ束縛している。
        # docs/reference/capital-allocation-assessment.md#research-source / refs #1481
        research_triage_as_of = str(research_triage.get("as_of", ""))
        if research_triage_as_of and assessment.as_of < date.fromisoformat(research_triage_as_of):
            raise CapitalAllocationConflictError(
                f"assessment as_of {assessment.as_of} precedes research triage "
                f"{research_triage_as_of}"
            )
        research_triage_tickers = (
            None
            if assessment.research_triage_id is None
            else (_admissible_research_tickers(research_triage, assessment.research_triage_id))
        )
        for alternative in assessment.alternatives:
            if (
                research_triage_tickers is not None
                and alternative.ticker not in research_triage_tickers
            ):
                raise CapitalAllocationConflictError(
                    f"alternative {alternative.ticker} is not researchable in "
                    f"{assessment.research_triage_id}"
                )
            stored = load_reviewed_thesis(connection, alternative.thesis_id)
            if stored.document.input_snapshot.ticker != alternative.ticker:
                raise CapitalAllocationConflictError(
                    f"thesis {alternative.thesis_id} ticker differs; not {alternative.ticker}"
                )
            if stored.core_sha256 != alternative.thesis_core_sha256:
                raise CapitalAllocationConflictError(
                    f"thesis {alternative.thesis_id} has moved since the draft was written"
                )
            if (
                alternative.thesis_review_id is not None
                and alternative.thesis_review_id != stored.review.review_id
            ):
                raise CapitalAllocationConflictError(
                    f"thesis {alternative.thesis_id} review ID differs from the stored review"
                )
            if alternative.disposition == "allocate":
                _, entry, _, _ = evaluate_allocation_in_transaction(
                    connection,
                    alternative=alternative,
                    assessment_id=assessment.capital_allocation_assessment_id,
                    as_of=assessment.as_of,
                    now=now,
                    sqlite_path=self._sqlite_path,
                    budget_max_yen=300_000,
                )
                if not entry.eligible:
                    raise CapitalAllocationConflictError("; ".join(entry.reasons))

    @staticmethod
    def _verify_research_set(
        connection: sqlite3.Connection, assessment: CapitalAllocationAssessment
    ) -> None:
        try:
            expected_scope = require_active_research_set(
                connection,
                research_triage_id=assessment.research_triage_id,
                tickers=(item.ticker for item in assessment.alternatives),
                published_at=assessment.published_at,
                scope=assessment.research_scope,
            )
            if expected_scope != assessment.research_scope:
                raise ValueError("CAA must bind completed and unfinished research scope")
            if expected_scope is not None:
                completed = {
                    item.ticker: item
                    for item in expected_scope.outcomes
                    if item.status == "reviewed"
                }
                for alternative in assessment.alternatives:
                    item = completed[alternative.ticker]
                    if (
                        alternative.thesis_id,
                        alternative.thesis_core_sha256,
                        alternative.thesis_review_id,
                    ) != (item.thesis_id, item.thesis_core_sha256, item.thesis_review_id):
                        raise ValueError(
                            "CAA differs from the Operation completed Reviewed Thesis references"
                        )
        except ValueError as error:
            raise CapitalAllocationConflictError(str(error)) from error

    def _research_triage(
        self, connection: sqlite3.Connection, research_triage_id: str
    ) -> dict[str, object]:
        row = connection.execute(
            "SELECT payload FROM research_triage WHERE research_triage_id = ?",
            (research_triage_id,),
        ).fetchone()
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

    def _existing_path(self) -> Path:
        path = database_path(self._db_path)
        if not path.is_file():
            raise CapitalAllocationConflictError(f"application database is unavailable: {path}")
        return path


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
