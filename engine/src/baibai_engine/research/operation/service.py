"""Application service for the single current operation workspace."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from baibai_engine.foundation.repository_layout import MARKET_DB_PATH, RUNS_DB_PATH
from baibai_engine.foundation.time import JST
from baibai_engine.research.capital_allocation import StoredCapitalAllocationAssessment
from baibai_engine.research.operation.models import (
    OperationPayload,
    OperationSession,
    OperationStatus,
    SessionKind,
)
from baibai_engine.research.operation.progress import (
    HoldingProposal,
    ResearchOutcome,
    ResearchProgress,
    ReviewedTarget,
    UnfinishedTarget,
    comparison_scope,
    progress_binding,
    selection_binding,
    with_progress,
)
from baibai_engine.research.operation.research_binding import (
    require_matching_research_set,
)
from baibai_engine.research.operation.validation import validate_progress, validate_selection
from baibai_engine.research.position_review import PositionReviewDocument
from baibai_engine.research.position_review_service import PositionReviewService
from baibai_engine.research.selection_policy import CURRENT_RESEARCH_CAPACITY
from baibai_engine.research.selection_query import verify_analysis_snapshot
from baibai_engine.research.thesis_store import load_reviewed_thesis
from baibai_engine.storage.application.json import canonical_json
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
        sqlite_path: Path = MARKET_DB_PATH,
        runs_path: Path = RUNS_DB_PATH,
    ) -> None:
        self._db_path = db_path
        self._sqlite_path = sqlite_path
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
        _require_current_payload(payload)
        if started_at.utcoffset() is None:
            raise OperationConflictError("started_at must include a UTC offset")
        selection = selection_binding(payload)
        if selection is None:
            raise OperationConflictError(
                "start Research with `research prepare`; one research_selection is required"
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
                    sqlite_path=self._sqlite_path,
                    complete=False,
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
        _require_current_payload(payload)
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
            raise OperationConflictError(
                "explicitly bind the historical active selection before research"
            )
        by_ticker = {item.ticker: item for item in progress.outcomes}
        by_ticker[outcome.ticker] = outcome
        # Selection order keeps idempotent retries from changing CAA scope identity.
        if outcome.ticker not in {item.ticker for item in selected.targets}:
            raise OperationConflictError("ticker is outside the fixed selection")
        updated = ResearchProgress(
            outcomes=tuple(
                by_ticker[item.ticker] for item in selected.targets if item.ticker in by_ticker
            )
        )
        payload = with_progress(operation.payload, updated)
        return self._replace_active(
            operation_id,
            payload=payload,
            completed_at=None,
            checked_at=now,
            expected_payload=operation.payload,
        )

    def record_thesis(
        self, operation_id: str, thesis_id: str, *, now: datetime
    ) -> OperationSession:
        with closing(connect_rw(self._db_path)) as connection:
            pair = load_reviewed_thesis(connection, thesis_id)
        return self.record_outcome(
            operation_id,
            ReviewedTarget(
                ticker=pair.document.input_snapshot.ticker,
                thesis_id=thesis_id,
                thesis_core_sha256=pair.core_sha256,
                thesis_review_id=pair.review.review_id,
            ),
            now=now,
        )

    def record_position_proposal(
        self, operation_id: str, draft: PositionReviewDocument, *, now: datetime
    ) -> OperationSession:
        operation = self.get(operation_id)
        if operation.status != "active":
            raise OperationConflictError("completed Operation is immutable")
        existing = next(
            (
                item
                for item in progress_binding(operation.payload).outcomes
                if item.ticker == draft.ticker
            ),
            None,
        )
        if isinstance(existing, HoldingProposal) and existing.draft.model_dump(
            exclude={"action"}
        ) == draft.model_dump(exclude={"action"}):
            return operation
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN")
            pair = load_reviewed_thesis(connection, draft.thesis_id)
            evaluation = PositionReviewService(sqlite_path=self._sqlite_path).check_in_transaction(
                connection, draft, now
            )
        return self.record_outcome(
            operation_id,
            HoldingProposal(
                ticker=draft.ticker,
                draft=draft.model_copy(update={"action": evaluation.action}),
                checked_at=now,
                thesis_core_sha256=pair.core_sha256,
                thesis_review_id=pair.review.review_id,
            ),
            now=now,
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
            raise OperationConflictError("this historical Operation has no fixed selection")
        target = next((item for item in selection.targets if item.ticker == ticker), None)
        if target is None:
            raise OperationConflictError("ticker is outside the fixed selection")
        return self.record_outcome(
            operation_id,
            UnfinishedTarget(
                ticker=ticker, purpose=target.purpose, reason=reason, references=references
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
        initialize_database(self._db_path)
        sql = "SELECT * FROM operation_session"
        parameters: tuple[object, ...] = ()
        if status is not None:
            sql += " WHERE status = ?"
            parameters = (status,)
        sql += " ORDER BY started_at DESC, operation_id DESC"
        with closing(connect_rw(self._db_path)) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [_from_row(row) for row in rows]

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
        _require_current_payload(payload)
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
                    raise OperationConflictError("historical Operation is read-only")
                if incoming != selected_before:
                    raise OperationConflictError(
                        "Research selection is immutable; no replacement targets"
                    )
                progress = validate_progress(
                    connection,
                    selected_before,
                    progress_binding(payload),
                    now=completed_at or checked_at or datetime.now(JST),
                    sqlite_path=self._sqlite_path,
                    complete=completed_at is not None,
                    previous=progress_binding(before.payload),
                )
                if progress.outcomes:
                    payload = with_progress(payload, progress)
                if completed_at is not None:
                    scope = comparison_scope(before.operation_id, payload)
                    if any(isinstance(item, ReviewedTarget) for item in scope.outcomes):
                        _require_published_assessment(
                            connection,
                            before.model_copy(update={"payload": payload}),
                            payload,
                            completed_at,
                        )
                    elif any(
                        item.get("kind") == "capital_allocation_assessment"
                        for item in payload.artifacts
                    ):
                        raise OperationCompletionError(
                            "no completed new research; do not fabricate CAA"
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


def _require_published_assessment(
    connection: sqlite3.Connection,
    operation: OperationSession,
    payload: OperationPayload,
    completed_at: datetime,
) -> StoredCapitalAllocationAssessment:
    references = [
        artifact.get("ref")
        for artifact in payload.artifacts
        if artifact.get("kind") == "capital_allocation_assessment"
    ]
    if len(references) != 1 or not isinstance(references[0], str) or not references[0]:
        raise OperationCompletionError(
            "completion requires one capital_allocation_assessment artifact/ref"
        )
    row = connection.execute(
        "SELECT payload FROM capital_allocation_assessment "
        "WHERE capital_allocation_assessment_id = ?",
        (references[0],),
    ).fetchone()
    if row is None:
        raise OperationCompletionError("canonical capital_allocation_assessment is unavailable")
    try:
        assessment = StoredCapitalAllocationAssessment.from_payload(json.loads(row["payload"]))
        expected_scope = require_matching_research_set(
            operation,
            research_triage_id=assessment.research_triage_id,
            tickers=(item.ticker for item in assessment.alternatives),
            published_at=assessment.published_at,
            scope=assessment.research_scope,
        )
        if expected_scope != assessment.research_scope:
            raise ValueError("assessment scope must bind completed and unfinished new research")
        if assessment.published_at > completed_at:
            raise ValueError("completion precedes assessment publication")
    except ValueError as error:
        raise OperationCompletionError(str(error)) from error
    return assessment


def _validate_complete(session_kind: SessionKind, payload: OperationPayload) -> None:
    missing: list[str] = []
    if payload.result is None:
        missing.append("result")
    if payload.next is None:
        missing.append("next")

    if not payload.artifacts or any(not artifact for artifact in payload.artifacts):
        missing.append("artifacts")
    if missing:
        raise OperationCompletionError(f"{session_kind} completion requires: {', '.join(missing)}")


def _require_current_payload(payload: OperationPayload) -> None:
    if payload.completion_reason is not None:
        raise OperationCompletionError(
            "no-research is read-only history; "
            "current empty selections use explicit research_selection"
        )


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
