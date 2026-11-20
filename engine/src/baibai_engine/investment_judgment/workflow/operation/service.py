"""Application service for the single current operation workspace."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from baibai_engine.investment_judgment.stock_assessment.document import AssessmentRef
from baibai_engine.investment_judgment.stock_assessment.store import load_reviewed_assessment
from baibai_engine.investment_judgment.trade_proposal.query import (
    has_published_proposal_for_operation,
    is_erroneous_proposal_as_of,
    load_investment_proposal,
    load_proposal_with_assessments,
)
from baibai_engine.investment_judgment.workflow.operation.admission import (
    freeze_runs,
    select_in_app,
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
    validate_target_purposes,
)
from baibai_engine.investment_judgment.workflow.selection import (
    GeneratedResearchSelectionDraft,
    GeneratedResearchSelectionV5,
    ResearchSelectionRecord,
    SelectedTarget,
    validate_input_known_at,
)
from baibai_engine.investment_judgment.workflow.selection_policy import (
    research_capacity,
    validate_capacity,
)
from baibai_engine.primitives.json import encode_json
from baibai_engine.primitives.time import JST
from baibai_engine.storage.paths import RUNS_DB_PATH
from baibai_engine.storage.sqlite.application.paths import database_path
from baibai_engine.storage.sqlite.application.read import connect_read_only
from baibai_engine.storage.sqlite.application.write import (
    connect_existing,
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
        draft: GeneratedResearchSelectionDraft,
        *,
        now: datetime,
    ) -> tuple[OperationSession, GeneratedResearchSelectionV5]:
        if not isinstance(draft, GeneratedResearchSelectionDraft):
            raise OperationConflictError(
                "start with a generated selection draft; saved selections are resume-only"
            )
        # model_copy/constructはfield検証を迂回できる。再検証は小さい依頼だけ。
        draft = GeneratedResearchSelectionDraft.model_validate(draft.model_dump(mode="python"))
        try:
            validate_input_known_at(draft.input_known_at, now=now)
            validate_capacity(
                len(draft.targets), research_capacity(draft.request.scope, len(draft.targets))
            )
        except ValueError as error:
            raise OperationConflictError(str(error)) from error
        fixed = freeze_runs(draft, self._runs_path)
        initialize_database(self._db_path)
        with closing(connect_existing(self._db_path)) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT operation_id FROM operation_session WHERE status='active'"
            ).fetchone()
            if active is not None:
                raise OperationConflictError(f"active operation already exists: {active[0]}")
            try:
                validate_target_purposes(connection, draft.targets, now=now)
            except ValueError as error:
                raise OperationConflictError(str(error)) from error
            selection = select_in_app(connection, draft, fixed)
            holding = draft.request.scope == "review_positions"
            header = OperationHeader(
                operation_id=f"research-op-{uuid4().hex}",
                session_kind="position-review" if holding else "capital-allocation",
                status="active",
                as_of=draft.research_as_of,
                ticker=draft.targets[0].ticker if holding and len(draft.targets) == 1 else None,
                started_at=now,
            )
            payload = OperationPayload(
                checkpoint="Research selection and fixed inputs saved",
                artifacts=(selection.model_dump(mode="json"),),
                canonical_refs=(draft.generation_id,),
                next="research the selected targets; record completed or unfinished outcomes",
            )
            body = encode_json(payload.model_dump(mode="json"))
            del fixed, selection, payload
            _insert(connection, header, body)
            # 初回の自由形式JSONキー順もresumeと同じ保存バイトから投影する。
            # 読み直すのは本文だけ。RunsやApp正本への再照会はしない。
            committed = OperationSession(
                **dict(header), payload=OperationPayload.model_validate_json(body)
            )
            saved = selection_binding(committed.payload)
            assert isinstance(saved, GeneratedResearchSelectionV5)
        return committed, saved

    def checkpoint(
        self,
        operation_id: str,
        *,
        note: str,
        next: str | None = None,
        now: datetime,
    ) -> OperationSession:
        def update_notes(
            _connection: sqlite3.Connection,
            before: OperationSession,
            _selection: ResearchSelectionRecord,
            _previous: ResearchProgress,
        ) -> OperationPayload | None:
            if not note.strip() or (next is not None and not next.strip()):
                raise ValueError("checkpoint note and optional next action must not be blank")
            updates = {"checkpoint": note}
            if next is not None:
                updates["next"] = next
            if all(getattr(before.payload, key) == value for key, value in updates.items()):
                return None
            return before.payload.model_copy(update=updates)

        return self._edit_active(operation_id, update_notes, now=now)

    def complete(
        self,
        operation_id: str,
        *,
        result: str,
        next: str,
        completed_at: datetime,
        proposal_id: str | None = None,
    ) -> OperationSession:
        if not database_path(self._db_path).is_file():
            raise OperationNotFoundError(f"unknown operation_id: {operation_id}")
        try:
            with closing(connect_existing(self._db_path)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM operation_session WHERE operation_id=?", (operation_id,)
                ).fetchone()
                if row is None:
                    raise OperationNotFoundError(f"unknown operation_id: {operation_id}")
                before = _from_row(row)
                if completed_at.utcoffset() is None:
                    raise OperationCompletionError("completion instant must include a UTC offset")
                progress = progress_binding(before.payload)
                reference = progress.proposal_ref
                if before.status == "completed":
                    if (
                        before.payload.result == result
                        and before.payload.next == next
                        and (None if reference is None else reference.proposal_id) == proposal_id
                        and before.completed_at is not None
                        and completed_at >= before.completed_at
                    ):
                        # 応答喪失後も当時の参照・時計を返す。後日訂正で完了を書き換えない。
                        return before
                    raise OperationConflictError(
                        f"completed operation session is immutable: {operation_id}"
                    )
                selection = selection_binding(before.payload)
                if selection is None:
                    raise OperationCompletionError("fixed research_selection is required")
                payload = before.payload.model_copy(update={"result": result, "next": next})
                payload.validate_metadata()
                _validate_complete(before.session_kind, payload)
                if proposal_id is None:
                    validate_progress(
                        connection, selection, progress, now=completed_at, complete=True
                    )
                    if (
                        reference is not None
                        or any(outcome.status == "reviewed" for outcome in progress.outcomes)
                        or has_published_proposal_for_operation(connection, operation_id)
                    ):
                        raise OperationCompletionError(
                            "completion requires one published Trade Proposal reference "
                            "(--proposal-id)"
                        )
                else:
                    exact = _completion_reference(
                        connection,
                        before,
                        selection,
                        progress,
                        proposal_id=proposal_id,
                        completed_at=completed_at,
                    )
                    if reference != exact:
                        payload = with_progress(
                            payload, progress.model_copy(update={"proposal_ref": exact})
                        )
                operation = OperationSession.model_validate(
                    {
                        **before.model_dump(mode="json", exclude={"payload"}),
                        "status": "completed",
                        "completed_at": completed_at,
                        "payload": payload,
                    }
                )
                connection.execute(
                    "UPDATE operation_session SET status='completed', completed_at=?, payload=? "
                    "WHERE operation_id=?",
                    (
                        completed_at.isoformat(),
                        encode_json(payload.model_dump(mode="json")),
                        operation_id,
                    ),
                )
                return operation
        except (OperationCompletionError, OperationConflictError, OperationNotFoundError):
            raise
        except (ValueError, FileNotFoundError) as error:
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
        self, operation_id: str, edit: _Edit, *, now: datetime | None
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
                complete=False,
                previous=previous,
            )
            if (previous_ref := progress.proposal_ref) is not None:
                # 旧activeの公開参照は保持する。後着outcomeの再評価は明示completeの責務。
                proposal = load_investment_proposal(connection, previous_ref.proposal_id)
                if (
                    proposal.review.draft_sha256 != previous_ref.draft_sha256
                    or proposal.research_scope.operation_id != operation_id
                    or proposal.published_at > checked_at
                ):
                    raise OperationConflictError(
                        "saved Proposal reference, Operation or publication time differs"
                    )
            operation = before.model_copy(update={"payload": payload})
            connection.execute(
                "UPDATE operation_session SET payload=? WHERE operation_id=?",
                (encode_json(payload.model_dump(mode="json")), operation_id),
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


def _completion_reference(
    connection: sqlite3.Connection,
    operation: OperationSession,
    selection: ResearchSelectionRecord,
    progress: ResearchProgress,
    *,
    proposal_id: str,
    completed_at: datetime,
) -> ProposalRef:
    reference = progress.proposal_ref
    if not selection.targets:
        raise OperationCompletionError(
            "empty selection ends with a reason; do not fabricate a Proposal"
        )
    if is_erroneous_proposal_as_of(connection, proposal_id, completed_at):
        raise OperationCompletionError("Proposal was corrected before completion")
    # 正式readerが本文・査読hash・indexと評価参照を照合する。最新には乗り換えない。
    proposal, pairs = load_proposal_with_assessments(connection, proposal_id)
    if proposal.research_scope.operation_id != operation.operation_id:
        raise OperationCompletionError("Proposal belongs to another Research Operation")
    if proposal.published_at > completed_at:
        raise OperationCompletionError("research progress precedes Proposal publication")
    if proposal.published_at < operation.started_at:
        raise OperationCompletionError(
            "Proposal publication precedes the active Research Operation"
        )
    if selection.request.scope != proposal.request_scope:
        raise OperationCompletionError("Proposal request scope differs from the Research selection")
    if fixed_proposal_scope(operation.operation_id, selection, progress) != proposal.research_scope:
        raise OperationCompletionError("Proposal scope differs from the Operation's fixed coverage")
    if any(pair.published_at > proposal.published_at for pair in pairs.values()):
        raise OperationCompletionError("Assessment is later than Proposal publication")
    if any(item.action == "buy" and item.terms is None for item in proposal.items):
        raise OperationCompletionError("buy proposal completion requires concrete order terms")
    exact = ProposalRef(proposal_id=proposal_id, draft_sha256=proposal.review.draft_sha256)
    if reference is not None and reference.proposal_id == proposal_id and reference != exact:
        raise OperationCompletionError("Proposal reference differs from its saved draft hash")
    return exact


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


def _insert(connection: sqlite3.Connection, operation: OperationHeader, payload_json: str) -> None:
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
            payload_json,
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
