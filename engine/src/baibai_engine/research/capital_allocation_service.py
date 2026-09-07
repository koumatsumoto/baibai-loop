"""Assessment publication service — verified judgment rowsを産む write boundary。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.paths import database_path
from baibai_engine.appdb.read import connect_read_only
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.foundation.repository_layout import MARKET_DB_PATH
from baibai_engine.foundation.time import JST
from baibai_engine.operation.research_binding import require_active_research_set

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
                    stored = CapitalAllocationAssessment.model_validate(json.loads(str(row[0])))
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
        if assessment.as_of != now.date():
            raise CapitalAllocationConflictError("formal assessment basis must be today")
        expected_draft = capital_allocation_draft_sha256(assessment)
        if require_review_binding and assessment.review.draft_sha256 != expected_draft:
            raise CapitalAllocationConflictError(
                f"content review binds a different draft, expected {expected_draft}"
            )
        research_triage = self._research_triage(connection, assessment.research_triage_id)
        # Validate the authored draft before replacing its timestamp, so a draft
        # from before this Operation cannot be made current by the writer's clock.
        self._verify_research_set(
            connection,
            assessment.model_copy(update={"published_at": min(assessment.published_at, now)}),
        )
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
            stored = load_reviewed_thesis(connection, alternative.thesis_id)
            if stored.document.input_snapshot.ticker != alternative.ticker:
                raise CapitalAllocationConflictError(
                    f"thesis {alternative.thesis_id} ticker differs; not {alternative.ticker}"
                )
            if stored.core_sha256 != alternative.thesis_core_sha256:
                raise CapitalAllocationConflictError(
                    f"thesis {alternative.thesis_id} has moved since the draft was written"
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
            require_active_research_set(
                connection,
                research_triage_id=assessment.research_triage_id,
                tickers=(item.ticker for item in assessment.alternatives),
                published_at=assessment.published_at,
            )
        except ValueError as error:
            raise CapitalAllocationConflictError(str(error)) from error

    def historical_allocated_ticker(self, assessment_id: str) -> str:
        """Resolve recorded order identity without replaying past investment eligibility."""
        with closing(connect_read_only(self._existing_path())) as connection:
            row = connection.execute(
                "SELECT result, payload FROM capital_allocation_assessment "
                "WHERE capital_allocation_assessment_id = ?",
                (assessment_id,),
            ).fetchone()
        if row is None or row[0] != "allocate":
            raise CapitalAllocationConflictError("assessment is not an allocate decision")
        payload = json.loads(str(row[1]))
        if not isinstance(payload, dict) or payload.get("result") != "allocate":
            raise CapitalAllocationConflictError("invalid historical allocation identity")
        alternatives = payload.get("alternatives")
        if not isinstance(alternatives, list):
            raise CapitalAllocationConflictError("missing historical alternatives")
        allocated = [
            item
            for item in alternatives
            if isinstance(item, dict) and item.get("disposition") == "allocate"
        ]
        if len(allocated) != 1 or not isinstance(allocated[0].get("ticker"), str):
            raise CapitalAllocationConflictError("ambiguous historical allocation identity")
        return str(allocated[0]["ticker"])

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
