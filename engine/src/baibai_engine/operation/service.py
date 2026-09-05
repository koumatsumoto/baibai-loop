"""Application service for the single current operation workspace."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.research.capital_allocation import CapitalAllocationAssessment

from .models import OperationPayload, OperationSession, OperationStatus, SessionKind
from .research_binding import require_matching_research_set, research_binding


class OperationNotFoundError(ValueError):
    pass


class OperationConflictError(ValueError):
    pass


class OperationCompletionError(ValueError):
    pass


class OperationService:
    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

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
        if session_kind == "position-review" and (ticker is None or not ticker.strip()):
            raise OperationConflictError("position-review start requires ticker")
        if session_kind == "capital-allocation":
            try:
                research_binding(payload)
            except ValueError as error:
                raise OperationConflictError(
                    f"{error}; start Research with `research prepare`"
                ) from error
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
                operation_id = _next_id(connection, as_of=as_of, session_kind=session_kind)
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

    def checkpoint(self, operation_id: str, payload: OperationPayload) -> OperationSession:
        return self._replace_active(operation_id, payload=payload, completed_at=None)

    def complete(
        self,
        operation_id: str,
        payload: OperationPayload,
        *,
        completed_at: datetime,
    ) -> OperationSession:
        _require_current_payload(payload)
        return self._replace_active(operation_id, payload=payload, completed_at=completed_at)

    def get(self, operation_id: str) -> OperationSession:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            row = connection.execute(
                "SELECT * FROM operation_session WHERE operation_id = ?", (operation_id,)
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
        operations = self.list(status="active")
        return None if not operations else operations[0]

    def _replace_active(
        self,
        operation_id: str,
        *,
        payload: OperationPayload,
        completed_at: datetime | None,
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
                if before.status != "active":
                    raise OperationConflictError(
                        f"completed operation session is immutable: {operation_id}"
                    )
                # Checkpoints replace prose, but cannot replace the human admission.
                if before.session_kind == "capital-allocation" and any(
                    item.get("kind") == "research_triage" for item in before.payload.artifacts
                ):
                    try:
                        if research_binding(before.payload) != research_binding(payload):
                            raise ValueError("Research Set binding is immutable")
                    except ValueError as error:
                        raise OperationConflictError(str(error)) from error
                if completed_at is not None:
                    _validate_complete(before.session_kind, payload)
                    if before.session_kind == "capital-allocation":
                        _require_published_assessment(connection, before, payload, completed_at)
                    elif before.session_kind == "position-review":
                        _require_published_position_review(connection, before, payload)
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
                        canonical_json(before.payload.model_dump(mode="json")),
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
) -> None:
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
        assessment = CapitalAllocationAssessment.model_validate(json.loads(row["payload"]))
        require_matching_research_set(
            operation,
            research_triage_id=assessment.research_triage_id,
            tickers=(item.ticker for item in assessment.alternatives),
            published_at=assessment.published_at,
        )
        if assessment.published_at > completed_at:
            raise ValueError("completion precedes assessment publication")
    except ValueError as error:
        raise OperationCompletionError(str(error)) from error


def _require_published_position_review(
    connection: sqlite3.Connection, operation: OperationSession, payload: OperationPayload
) -> None:
    if len(payload.canonical_refs) != 1:
        raise OperationCompletionError("completion requires one canonical Position Review ID")
    row = connection.execute(
        "SELECT ticker, as_of FROM position_review WHERE position_review_id = ?",
        (payload.canonical_refs[0],),
    ).fetchone()
    if row is None:
        raise OperationCompletionError("canonical Position Review is unavailable")
    if row["ticker"] != operation.ticker or row["as_of"] != operation.as_of.isoformat():
        raise OperationCompletionError("Position Review ticker/as_of differs from the Operation")


def _validate_complete(session_kind: SessionKind, payload: OperationPayload) -> None:
    missing: list[str] = []
    if payload.result is None:
        missing.append("result")
    if payload.next is None:
        missing.append("next")

    if (
        payload.human_confirmation is None
        or payload.human_confirmation.request is None
        or payload.human_confirmation.result is None
    ):
        missing.append("human_confirmation.request/result")
    if session_kind in {"capital-allocation", "position-review"} and (
        not payload.artifacts or any(not artifact for artifact in payload.artifacts)
    ):
        missing.append("artifacts")
    if session_kind == "position-review" and not payload.canonical_refs:
        missing.append("canonical_refs")
    if missing:
        raise OperationCompletionError(f"{session_kind} completion requires: {', '.join(missing)}")


def _require_current_payload(payload: OperationPayload) -> None:
    if payload.completion_reason is not None:
        raise OperationCompletionError(
            "no-research is read-only history; an empty Research Set creates no Operation"
        )


def _next_id(
    connection: sqlite3.Connection,
    *,
    as_of: date,
    session_kind: SessionKind,
) -> str:
    slug = re.sub(r"[^a-z]+", "-", session_kind).strip("-")
    prefix = f"op-{as_of:%Y%m%d}-{slug}"
    sequence = (
        int(
            connection.execute(
                "SELECT count(*) FROM operation_session WHERE operation_id LIKE ?", (f"{prefix}-%",)
            ).fetchone()[0]
        )
        + 1
    )
    while connection.execute(
        "SELECT 1 FROM operation_session WHERE operation_id = ?", (f"{prefix}-{sequence}",)
    ).fetchone():
        sequence += 1
    return f"{prefix}-{sequence}"


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
