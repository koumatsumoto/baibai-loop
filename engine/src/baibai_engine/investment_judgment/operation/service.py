"""Application service for the single current operation workspace."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from baibai_engine.foundation.json import canonical_json
from baibai_engine.foundation.repository_layout import RUNS_DB_PATH
from baibai_engine.foundation.sqlite_read import read_rows
from baibai_engine.foundation.time import JST
from baibai_engine.investment_judgment.operation.models import (
    OperationPayload,
    OperationSession,
    OperationStatus,
    SessionKind,
)
from baibai_engine.investment_judgment.operation.progress import (
    ProposalRef,
    ResearchOutcome,
    ResearchProgress,
    ReviewedTarget,
    UnfinishedTarget,
    progress_binding,
    proposal_scope,
    selection_binding,
    with_progress,
)
from baibai_engine.investment_judgment.operation.validation import (
    validate_progress,
    validate_selection,
)
from baibai_engine.investment_judgment.selection_policy import CURRENT_RESEARCH_CAPACITY
from baibai_engine.investment_judgment.selection import GeneratedResearchSelection
from baibai_engine.investment_judgment.generated_inputs import verify_generated_inputs
from baibai_engine.machine_analysis.screening.selection_store import ScreeningSelectionStore
from baibai_engine.investment_judgment.selection_query import verify_analysis_snapshot
from baibai_engine.investment_judgment.stock_assessment import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment_store import load_reviewed_assessment
from baibai_engine.investment_judgment.trade_proposal import TradeProposal, proposal_draft_sha256
from baibai_engine.investment_judgment.trade_proposal_query import load_investment_proposal
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only
from baibai_engine.storage.application.write import connect_rw, initialize_database


class OperationNotFoundError(ValueError):
    pass


class OperationConflictError(ValueError):
    pass


class OperationCompletionError(ValueError):
    pass


class OperationService:
    def __init__(
        self,
        db_path: Path | None = None,
        *,
        runs_path: Path = RUNS_DB_PATH,
    ) -> None:
        self._db_path = db_path
        self._runs_path = runs_path

    def start(
        self,
        *,
        session_kind: SessionKind,
        as_of: date,
        started_at: datetime,
        payload: OperationPayload,
        ticker: str | None = None,
    ) -> OperationSession:
        if started_at.utcoffset() is None:
            raise OperationConflictError("started_at must include a UTC offset")
        selection = selection_binding(payload)
        if selection is None:
            raise OperationConflictError(
                "start with `investment-judgment prepare`; one research_selection is required"
            )
        if selection.capacity != CURRENT_RESEARCH_CAPACITY:
            raise OperationConflictError("new Research requires the current capacity policy")
        if selection.input_known_at > started_at or selection.research_as_of != as_of:
            raise OperationConflictError("Research date/cutoff differs from Operation start")
        expected_kind = (
            "position-review"
            if selection.request.scope == "review_positions"
            else "capital-allocation"
        )
        expected_ticker = (
            selection.targets[0].ticker
            if expected_kind == "position-review" and len(selection.targets) == 1
            else None
        )
        if session_kind != expected_kind or ticker != expected_ticker:
            raise OperationConflictError("Operation kind/ticker differs from Research scope")
        if isinstance(selection, GeneratedResearchSelection):
            verify_generated_inputs(selection.inputs.generation, runs_path=self._runs_path)
            selection_id = selection.inputs.screening_selection_id
            if selection_id is not None:
                screening = ScreeningSelectionStore(self._runs_path).read(selection_id)
                if screening is None or screening.generation_id != selection.inputs.generation.generation_id:
                    raise OperationConflictError("screening selection differs from chosen generation")
        else:
            verify_analysis_snapshot(
                selection.inputs.analysis,
                tickers=tuple(t.ticker for t in selection.targets),
                known_at=selection.input_known_at,
                runs_path=self._runs_path,
            )
        if ticker is not None and re.fullmatch(r"[0-9A-Z]{4}", ticker) is None:
            raise OperationConflictError(
                "current ticker must be a four-character alphanumeric code"
            )
        if session_kind == "position-review" and any(
            item.purpose != "holding" for item in selection.targets
        ):
            raise OperationConflictError("position-review Operation admits only holding targets")
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                active = connection.execute(
                    "SELECT operation_id FROM operation_session WHERE status = 'active'"
                ).fetchone()
                if active is not None:
                    raise OperationConflictError(
                        f"active operation already exists: {active['operation_id']}"
                    )
                try:
                    validate_selection(connection, selection, now=started_at)
                except ValueError as error:
                    raise OperationConflictError(str(error)) from error
                progress = validate_progress(
                    connection,
                    selection,
                    progress_binding(payload),
                    now=started_at,
                    complete=False,
                )
                if progress.proposal_ref is not None:
                    raise OperationConflictError(
                        "bind a Proposal after starting its Research Operation"
                    )
                if progress.outcomes:
                    payload = with_progress(payload, progress)
                operation_id = f"research-op-{uuid4().hex}"
                operation = OperationSession(
                    operation_id=operation_id,
                    session_kind=session_kind,
                    status="active",
                    as_of=as_of,
                    ticker=ticker,
                    started_at=started_at,
                    payload=payload,
                )
                _insert(connection, operation)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return operation

    def checkpoint(
        self, operation_id: str, payload: OperationPayload, *, now: datetime | None = None
    ) -> OperationSession:
        return self._replace_active(
            operation_id, payload=payload, completed_at=None, checked_at=now
        )

    def complete(
        self,
        operation_id: str,
        payload: OperationPayload,
        *,
        completed_at: datetime,
    ) -> OperationSession:
        try:
            return self._replace_active(operation_id, payload=payload, completed_at=completed_at)
        except (OperationCompletionError, OperationConflictError):
            raise
        except ValueError as error:
            raise OperationCompletionError(str(error)) from error

    def record_outcome(
        self, operation_id: str, outcome: ResearchOutcome, *, now: datetime
    ) -> OperationSession:
        operation = self.get(operation_id)
        if operation.status != "active":
            raise OperationConflictError("completed Operation is immutable")
        progress = progress_binding(operation.payload)
        if outcome in progress.outcomes:
            return operation
        selected = selection_binding(operation.payload)
        if selected is None:
            raise OperationConflictError("fixed research_selection is required")
        by_ticker = {item.ticker: item for item in progress.outcomes}
        by_ticker[outcome.ticker] = outcome
        # Selection order gives retry-stable immutable Proposal scope identity.
        if outcome.ticker not in {item.ticker for item in selected.targets}:
            raise OperationConflictError("ticker is outside the fixed selection")
        updated = ResearchProgress(
            outcomes=tuple(
                by_ticker[item.ticker] for item in selected.targets if item.ticker in by_ticker
            ),
            proposal_ref=progress.proposal_ref,
        )
        payload = with_progress(operation.payload, updated)
        return self._replace_active(
            operation_id,
            payload=payload,
            completed_at=None,
            checked_at=now,
            expected_payload=operation.payload,
        )

    def record_assessment(
        self, operation_id: str, assessment_id: str, *, now: datetime
    ) -> OperationSession:
        operation = self.get(operation_id)
        if operation.status != "active":
            raise OperationConflictError("completed Operation is immutable")
        with closing(connect_read_only(self._db_path)) as connection:
            pair = load_reviewed_assessment(connection, assessment_id)
        selection = selection_binding(operation.payload)
        if selection is None:
            raise OperationConflictError("fixed research_selection is required")
        target = next(
            (
                item
                for item in selection.targets
                if item.ticker == pair.document.input_snapshot.ticker
            ),
            None,
        )
        if target is None:
            raise OperationConflictError("ticker is outside the fixed selection")
        return self.record_outcome(
            operation_id,
            ReviewedTarget(
                ticker=target.ticker,
                purpose=target.purpose,
                assessment_ref=AssessmentRef(
                    assessment_id=pair.assessment_id,
                    core_sha256=pair.core_sha256,
                    review_id=pair.review.review_id,
                ),
            ),
            now=now,
        )

    def record_proposal(
        self, operation_id: str, proposal_id: str, *, now: datetime
    ) -> OperationSession:
        operation = self.get(operation_id)
        if operation.status != "active":
            raise OperationConflictError("completed Operation is immutable")
        with closing(connect_read_only(self._db_path)) as connection:
            proposal = load_investment_proposal(connection, proposal_id)
        if proposal.research_scope.operation_id != operation_id:
            raise OperationConflictError("Proposal belongs to another Research Operation")
        progress = progress_binding(operation.payload)
        reference = ProposalRef(
            proposal_id=proposal.proposal_id, draft_sha256=proposal_draft_sha256(proposal)
        )
        updated = progress.model_copy(update={"proposal_ref": reference})
        return self._replace_active(
            operation_id,
            payload=with_progress(operation.payload, updated),
            completed_at=None,
            checked_at=now,
            expected_payload=operation.payload,
        )

    def record_unfinished(
        self,
        operation_id: str,
        *,
        ticker: str,
        reason: str,
        references: tuple[str, ...],
        now: datetime,
    ) -> OperationSession:
        selection = selection_binding(self.get(operation_id).payload)
        if selection is None:
            raise OperationConflictError("fixed research_selection is required")
        target = next((item for item in selection.targets if item.ticker == ticker), None)
        if target is None:
            raise OperationConflictError("ticker is outside the fixed selection")
        return self.record_outcome(
            operation_id,
            UnfinishedTarget(
                ticker=ticker,
                purpose=target.purpose,
                unfinished_reason=reason,
                references=references,
            ),
            now=now,
        )

    def get(self, operation_id: str) -> OperationSession:
        if not database_path(self._db_path).is_file():
            raise OperationNotFoundError(f"unknown operation_id: {operation_id}")
        with closing(connect_read_only(self._db_path)) as connection:
            row = connection.execute(
                "SELECT * FROM operation_session WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise OperationNotFoundError(f"unknown operation_id: {operation_id}")
        return _from_row(row)

    def list(self, *, status: OperationStatus | None = None) -> list[OperationSession]:
        sql = "SELECT * FROM operation_session"
        parameters: tuple[object, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            parameters = (status,)
        rows = read_rows(database_path(self._db_path), sql, parameters, connector=connect_read_only)
        return sorted(
            (_from_row(row) for row in rows),
            key=lambda operation: (operation.started_at, operation.operation_id),
            reverse=True,
        )

    def active(self) -> OperationSession | None:
        path = database_path(self._db_path)
        if not path.is_file():
            return None
        with closing(connect_read_only(path)) as connection:
            row = connection.execute(
                "SELECT * FROM operation_session WHERE status='active'"
            ).fetchone()
        return None if row is None else _from_row(row)

    def _replace_active(
        self,
        operation_id: str,
        *,
        payload: OperationPayload,
        completed_at: datetime | None,
        checked_at: datetime | None = None,
        expected_payload: OperationPayload | None = None,
    ) -> OperationSession:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM operation_session WHERE operation_id = ?", (operation_id,)
                ).fetchone()
                if row is None:
                    raise OperationNotFoundError(f"unknown operation_id: {operation_id}")
                before = _from_row(row)
                if expected_payload is not None and before.payload != expected_payload:
                    raise OperationConflictError(
                        "research progress changed; reload before recording outcome"
                    )
                if before.status != "active":
                    raise OperationConflictError(
                        f"completed operation session is immutable: {operation_id}"
                    )
                selected_before = selection_binding(before.payload)
                try:
                    incoming = selection_binding(payload)
                except ValueError as error:
                    raise OperationConflictError(str(error)) from error
                if selected_before is None:
                    raise OperationConflictError("fixed research_selection is required")
                if incoming != selected_before:
                    raise OperationConflictError(
                        "Research selection is immutable; no replacement targets"
                    )
                progress = validate_progress(
                    connection,
                    selected_before,
                    progress_binding(payload),
                    now=completed_at or checked_at or datetime.now(JST),
                    complete=completed_at is not None,
                    previous=progress_binding(before.payload),
                )
                if progress.outcomes:
                    payload = with_progress(payload, progress)
                if progress.proposal_ref is not None:
                    _require_published_proposal(
                        connection,
                        before.model_copy(update={"payload": payload}),
                        progress,
                        now=completed_at or checked_at or datetime.now(JST),
                        completing=completed_at is not None,
                        # Late outcomes preserve the old publication reference. A new
                        # binding/complete must use the current fully covered scope.
                        check_scope=completed_at is not None
                        or progress.proposal_ref != progress_binding(before.payload).proposal_ref,
                    )
                if completed_at is not None:
                    if selected_before.targets and progress.proposal_ref is None:
                        raise OperationCompletionError(
                            "completion requires one published Trade Proposal reference"
                        )
                    if not selected_before.targets and progress.proposal_ref is not None:
                        raise OperationCompletionError(
                            "empty selection ends with a reason; do not fabricate a Proposal"
                        )
                    _validate_complete(before.session_kind, payload)
                operation = OperationSession.model_validate(
                    {
                        **before.public(),
                        "status": "completed" if completed_at is not None else "active",
                        "completed_at": completed_at,
                        "payload": payload,
                    }
                )
                cursor = connection.execute(
                    """
                    UPDATE operation_session
                    SET status = ?, completed_at = ?, payload = ?
                    WHERE operation_id = ? AND status = 'active' AND payload = ?
                    """,
                    (
                        operation.status,
                        None if completed_at is None else completed_at.isoformat(),
                        canonical_json(payload.model_dump(mode="json")),
                        operation_id,
                        str(row["payload"]),
                    ),
                )
                if cursor.rowcount != 1:
                    raise OperationConflictError(
                        f"operation changed while replacing current payload: {operation_id}"
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return operation


def _require_published_proposal(
    connection: sqlite3.Connection,
    operation: OperationSession,
    progress: ResearchProgress,
    *,
    now: datetime,
    completing: bool,
    check_scope: bool,
) -> TradeProposal:
    reference = progress.proposal_ref
    assert reference is not None
    try:
        proposal = load_investment_proposal(connection, reference.proposal_id)
        if proposal_draft_sha256(proposal) != reference.draft_sha256:
            raise ValueError("Proposal reference differs from its saved draft hash")
        if proposal.research_scope.operation_id != operation.operation_id:
            raise ValueError("Proposal belongs to another Research Operation")
        if proposal.published_at > now:
            raise ValueError("research progress precedes Proposal publication")
        if check_scope:
            # The incoming progress is not persisted yet. Compare its exact scope
            # here; the publication service independently checks persisted progress.
            if proposal_scope(operation.operation_id, operation.payload) != proposal.research_scope:
                raise ValueError("Proposal scope differs from the Operation's fixed coverage")
            selection = selection_binding(operation.payload)
            assert selection is not None
            if proposal.request_scope != selection.request.scope:
                raise ValueError("Proposal request scope differs from the Research selection")
            if proposal.published_at < operation.started_at:
                raise ValueError("Proposal publication precedes the active Research Operation")
        if completing and any(
            item.action == "buy" and item.terms is None for item in proposal.items
        ):
            raise ValueError("buy proposal completion requires concrete order terms")
    except (ValueError, FileNotFoundError) as error:
        raise OperationCompletionError(str(error)) from error
    return proposal


def _validate_complete(session_kind: SessionKind, payload: OperationPayload) -> None:
    missing: list[str] = []
    if payload.result is None or not payload.result.strip():
        missing.append("result")
    if payload.next is None or not payload.next.strip():
        missing.append("next")

    if not payload.artifacts or any(not artifact for artifact in payload.artifacts):
        missing.append("artifacts")
    if missing:
        raise OperationCompletionError(f"{session_kind} completion requires: {', '.join(missing)}")


def _insert(connection: sqlite3.Connection, operation: OperationSession) -> None:
    connection.execute(
        """
        INSERT INTO operation_session (
            operation_id, session_kind, status, as_of, ticker,
            started_at, completed_at, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            operation.operation_id,
            operation.session_kind,
            operation.status,
            operation.as_of.isoformat(),
            operation.ticker,
            operation.started_at.isoformat(),
            None,
            canonical_json(operation.payload.model_dump(mode="json")),
        ),
    )


def _from_row(row: sqlite3.Row) -> OperationSession:
    return OperationSession.model_validate(
        {
            "operation_id": row["operation_id"],
            "session_kind": row["session_kind"],
            "status": row["status"],
            "as_of": row["as_of"],
            "ticker": row["ticker"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "payload": json.loads(str(row["payload"])),
        }
    )


__all__ = [
    "OperationCompletionError",
    "OperationConflictError",
    "OperationNotFoundError",
    "OperationService",
]
