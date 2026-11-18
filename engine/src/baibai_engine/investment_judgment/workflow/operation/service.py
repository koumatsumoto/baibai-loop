"""Application service for the single current operation workspace."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment.store import load_reviewed_assessment
from baibai_engine.investment_judgment.trade_proposal.contracts import (
    TradeProposal,
    proposal_draft_sha256,
)
from baibai_engine.investment_judgment.trade_proposal.query import (
    has_published_proposal_for_operation,
    load_investment_proposal,
)
from baibai_engine.investment_judgment.workflow.case_screening import (
    read_case_screening,
    same_case_screening,
)
from baibai_engine.investment_judgment.workflow.operation.models import (
    OperationHeader,
    OperationPayload,
    OperationSession,
    OperationStatus,
    SessionKind,
)
from baibai_engine.investment_judgment.workflow.operation.progress import (
    ProposalRef,
    ResearchOutcome,
    ResearchProgress,
    ReviewedTarget,
    UnfinishedTarget,
    fixed_proposal_scope,
    progress_binding,
    selection_binding,
    with_progress,
)
from baibai_engine.investment_judgment.workflow.operation.validation import (
    validate_progress,
    validate_selection,
)
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionV5,
    ResearchSelectionRecord,
    SelectedTarget,
    selection_sha256,
)
from baibai_engine.investment_judgment.workflow.selection_policy import CURRENT_RESEARCH_CAPACITY
from baibai_engine.machine_analysis.generation.query import read_research_model_inputs
from baibai_engine.primitives.json import encode_json
from baibai_engine.primitives.time import JST
from baibai_engine.storage.paths import RUNS_DB_PATH
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.write import (
    connect_existing,
    connect_rw,
    initialize_database,
)
from baibai_engine.storage.sqlite.read import read_rows

type _Edit = Callable[
    [sqlite3.Connection, OperationSession, ResearchSelectionRecord, ResearchProgress],
    OperationPayload | None,
]


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
        payload.validate_metadata()
        if started_at.utcoffset() is None:
            raise OperationConflictError("started_at must include a UTC offset")
        selection = selection_binding(payload)
        if selection is None:
            raise OperationConflictError(
                "start with `investment-judgment prepare`; one research_selection is required"
            )
        if not isinstance(selection, GeneratedResearchSelectionV5):
            raise OperationConflictError(
                "new Research requires Selection V5; legacy selections are resume-only"
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
        # 共有scopeは元の完全headerから解く。入力sliceの共有モデル欠落を見逃さない。
        canonical = read_research_model_inputs(
            selection.inputs.generation.generation_id,
            targets=selection.evidence_subjects(),
            path=self._runs_path,
        )
        if canonical is None or encode_json(canonical.model_dump(mode="json")) != encode_json(
            selection.inputs.generation.model_dump(mode="json")
        ):
            raise ValueError("model input slice differs from its canonical generation")
        del canonical
        selection_id = selection.inputs.screening_selection_id
        if selection_id is not None:
            proven = read_case_screening(
                selection_id,
                path=self._runs_path,
                known_at=selection.input_known_at,
                subjects=selection.evidence_subjects(),
            )
            if (
                proven is None
                or proven[0] != selection.inputs.screening_published_at
                or selection.inputs.screening_selection is None
                or not same_case_screening(selection.inputs.screening_selection, proven[1])
            ):
                raise OperationConflictError("screening selection differs from chosen generation")
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
                del selection
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
        payload.validate_metadata()

        def replace_notes(
            _connection: sqlite3.Connection,
            _before: OperationSession,
            selection: ResearchSelectionRecord,
            previous: ResearchProgress,
        ) -> OperationPayload:
            if progress_binding(payload) != previous:
                raise OperationConflictError(
                    "checkpoint cannot change research_progress; reload and use record-* commands"
                )
            # Only checkpoint accepts replacement artifacts from the caller.
            try:
                incoming = selection_binding(payload)
            except ValueError as error:
                raise OperationConflictError(str(error)) from error
            if incoming is None or selection_sha256(incoming) != selection_sha256(selection):
                raise OperationConflictError(
                    "Research selection is immutable; no replacement targets"
                )
            return payload

        return self._edit_active(operation_id, replace_notes, now=now)

    def complete(
        self,
        operation_id: str,
        *,
        result: str,
        next: str,
        completed_at: datetime,
    ) -> OperationSession:
        try:
            return self._edit_active(
                operation_id,
                lambda _conn, before, _selection, _previous: before.payload.model_copy(
                    update={"result": result, "next": next}
                ),
                now=completed_at,
                complete=True,
            )
        except (OperationCompletionError, OperationConflictError):
            raise
        except ValueError as error:
            raise OperationCompletionError(str(error)) from error

    def record_assessment(
        self, operation_id: str, assessment_id: str, *, now: datetime
    ) -> OperationSession:
        def bind(
            connection: sqlite3.Connection,
            before: OperationSession,
            selection: ResearchSelectionRecord,
            previous: ResearchProgress,
        ) -> OperationPayload | None:
            pair = load_reviewed_assessment(connection, assessment_id)
            target = _target(selection, pair.document.input_snapshot.ticker)
            return _outcome_payload(
                before,
                selection,
                previous,
                ReviewedTarget(
                    ticker=target.ticker,
                    purpose=target.purpose,
                    assessment_ref=AssessmentRef(
                        assessment_id=pair.assessment_id,
                        core_sha256=pair.core_sha256,
                        review_id=pair.review.review_id,
                    ),
                ),
            )

        return self._edit_active(operation_id, bind, now=now)

    def record_proposal(
        self, operation_id: str, proposal_id: str, *, now: datetime
    ) -> OperationSession:
        def bind(
            connection: sqlite3.Connection,
            before: OperationSession,
            _selection: ResearchSelectionRecord,
            previous: ResearchProgress,
        ) -> OperationPayload:
            proposal = load_investment_proposal(connection, proposal_id)
            if proposal.research_scope.operation_id != operation_id:
                raise OperationConflictError("Proposal belongs to another Research Operation")
            reference = ProposalRef(
                proposal_id=proposal.proposal_id, draft_sha256=proposal_draft_sha256(proposal)
            )
            return with_progress(
                before.payload, previous.model_copy(update={"proposal_ref": reference})
            )

        return self._edit_active(operation_id, bind, now=now)

    def record_unfinished(
        self,
        operation_id: str,
        *,
        ticker: str,
        reason: str,
        references: tuple[str, ...],
        now: datetime,
    ) -> OperationSession:
        def bind(
            _connection: sqlite3.Connection,
            before: OperationSession,
            selection: ResearchSelectionRecord,
            previous: ResearchProgress,
        ) -> OperationPayload | None:
            target = _target(selection, ticker)
            return _outcome_payload(
                before,
                selection,
                previous,
                UnfinishedTarget(
                    ticker=target.ticker,
                    purpose=target.purpose,
                    unfinished_reason=reason,
                    references=references,
                ),
            )

        return self._edit_active(operation_id, bind, now=now)

    def get(self, operation_id: str) -> OperationSession:
        with self._snapshot(operation_id) as (_connection, row):
            return _from_row(row)

    @contextmanager
    def _snapshot(self, operation_id: str) -> Iterator[tuple[sqlite3.Connection, sqlite3.Row]]:
        if not database_path(self._db_path).is_file():
            raise OperationNotFoundError(f"unknown operation_id: {operation_id}")
        with closing(connect_read_only(self._db_path)) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM operation_session WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None:
                raise OperationNotFoundError(f"unknown operation_id: {operation_id}")
            yield connection, row

    def list(self, *, status: OperationStatus | None = None) -> list[OperationHeader]:
        sql = (
            "SELECT operation_id, session_kind, status, as_of, ticker, started_at, completed_at "
            "FROM operation_session"
        )
        parameters: tuple[object, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            parameters = (status,)
        rows = read_rows(database_path(self._db_path), sql, parameters, connector=connect_read_only)
        return sorted(
            (OperationHeader.model_validate(dict(row)) for row in rows),
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

    def _edit_active(
        self, operation_id: str, edit: _Edit, *, now: datetime | None, complete: bool = False
    ) -> OperationSession:
        with self._snapshot(operation_id) as (reader, row):
            before = _from_row(row)
            if before.status != "active":
                raise OperationConflictError(
                    f"completed operation session is immutable: {operation_id}"
                )
            selection = selection_binding(before.payload)
            if selection is None:
                raise OperationConflictError("fixed research_selection is required")
            previous = progress_binding(before.payload)
            payload = edit(reader, before, selection, previous)
            if payload is None:
                return before
            expected_payload = row["payload"]
        del row
        if complete:
            payload.validate_metadata()
        with closing(connect_existing(self._db_path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            # Compare the original serialized snapshot under the writer lock. No second
            # payload has to enter Python, and another writer cannot intervene after this.
            if (
                connection.execute(
                    "SELECT 1 FROM operation_session WHERE operation_id=? "
                    "AND status='active' AND payload=?",
                    (operation_id, expected_payload),
                ).fetchone()
                is None
            ):
                raise OperationConflictError(
                    "research progress changed; reload before updating Operation"
                )
            del expected_payload
            checked_at = now if now is not None else datetime.now(JST)
            progress = validate_progress(
                connection,
                selection,
                progress_binding(payload),
                now=checked_at,
                complete=complete,
                previous=previous,
            )
            # Completion adds notes, not a new representation of stored artifacts.
            if progress.outcomes and not complete:
                payload = with_progress(payload, progress)
            if progress.proposal_ref is not None:
                _require_published_proposal(
                    connection,
                    before,
                    selection,
                    progress,
                    now=checked_at,
                    completing=complete,
                    # Late outcomes keep the old publication; binding/closing checks current scope.
                    check_scope=complete or progress.proposal_ref != previous.proposal_ref,
                )
            if complete:
                if (
                    selection.targets
                    and progress.proposal_ref is None
                    and (
                        not all(outcome.status == "unfinished" for outcome in progress.outcomes)
                        or has_published_proposal_for_operation(connection, operation_id)
                    )
                ):
                    raise OperationCompletionError(
                        "completion requires one published Trade Proposal reference"
                    )
                if not selection.targets and progress.proposal_ref is not None:
                    raise OperationCompletionError(
                        "empty selection ends with a reason; do not fabricate a Proposal"
                    )
                _validate_complete(before.session_kind, payload)
            del selection
            operation = OperationSession.model_validate(
                {
                    **before.model_dump(mode="json", exclude={"payload"}),
                    "status": "completed" if complete else "active",
                    "completed_at": now if complete else None,
                    "payload": payload,
                }
            )
            connection.execute(
                "UPDATE operation_session SET status=?, completed_at=?, payload=? "
                "WHERE operation_id=?",
                (
                    operation.status,
                    operation.completed_at.isoformat() if operation.completed_at else None,
                    encode_json(payload.model_dump(mode="json")),
                    operation_id,
                ),
            )
            return operation


def _target(selection: ResearchSelectionRecord, ticker: str) -> SelectedTarget:
    for target in selection.targets:
        if target.ticker == ticker:
            return target
    raise OperationConflictError("ticker is outside the fixed selection")


def _outcome_payload(
    before: OperationSession,
    selection: ResearchSelectionRecord,
    previous: ResearchProgress,
    outcome: ResearchOutcome,
) -> OperationPayload | None:
    if outcome in previous.outcomes:
        return None
    by_ticker = {item.ticker: item for item in previous.outcomes}
    by_ticker[outcome.ticker] = outcome
    # Selection order gives retry-stable immutable Proposal scope identity.
    return with_progress(
        before.payload,
        ResearchProgress(
            outcomes=tuple(by_ticker[t.ticker] for t in selection.targets if t.ticker in by_ticker),
            proposal_ref=previous.proposal_ref,
        ),
    )


def _require_published_proposal(
    connection: sqlite3.Connection,
    operation: OperationSession,
    selection: ResearchSelectionRecord,
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
            if (
                fixed_proposal_scope(operation.operation_id, selection, progress)
                != proposal.research_scope
            ):
                raise ValueError("Proposal scope differs from the Operation's fixed coverage")
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
            encode_json(operation.payload.model_dump(mode="json")),
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
