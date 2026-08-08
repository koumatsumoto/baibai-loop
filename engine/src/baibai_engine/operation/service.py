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

from .models import OperationPayload, OperationSession, OperationStatus, SessionKind


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
        operation = self.get(operation_id)
        _validate_complete(operation.session_kind, payload)
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
                if (
                    completed_at is not None
                    and payload.completion_reason == "no-shortlist-selection"
                ):
                    _validate_zero_selection_publication(
                        connection,
                        payload,
                        as_of=before.as_of,
                    )
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


def _validate_complete(session_kind: SessionKind, payload: OperationPayload) -> None:
    missing: list[str] = []
    if payload.result is None:
        missing.append("result")
    if payload.next is None:
        missing.append("next")

    no_shortlist_selection = (
        session_kind == "opportunity" and payload.completion_reason == "no-shortlist-selection"
    )
    if payload.completion_reason is not None and not no_shortlist_selection:
        missing.append("completion_reason valid for this session kind")
    if no_shortlist_selection:
        if payload.human_confirmation is not None:
            missing.append("human_confirmation omitted for no-shortlist-selection")
        if not _has_zero_selection_shortlist(payload):
            missing.append("shortlist artifact with selected_count=0")
        if not payload.canonical_refs:
            missing.append("canonical_refs")

    requires_confirmation = not no_shortlist_selection and session_kind in {
        "opportunity",
        "pending-result",
        "monthly-contribution",
        "earnings-material-event",
    }
    if requires_confirmation and (
        payload.human_confirmation is None
        or payload.human_confirmation.request is None
        or payload.human_confirmation.result is None
    ):
        missing.append("human_confirmation.request/result")
    if session_kind in {"opportunity", "earnings-material-event"} and (
        not payload.artifacts or any(not artifact for artifact in payload.artifacts)
    ):
        missing.append("artifacts")
    if (
        session_kind
        in {
            "pending-result",
            "monthly-contribution",
            "earnings-material-event",
            "annual-outcome",
        }
        and not payload.canonical_refs
    ):
        missing.append("canonical_refs")
    if missing:
        raise OperationCompletionError(f"{session_kind} completion requires: {', '.join(missing)}")


def _has_zero_selection_shortlist(payload: OperationPayload) -> bool:
    return _zero_selection_reference(payload) is not None


def _zero_selection_reference(payload: OperationPayload) -> str | None:
    for artifact in payload.artifacts:
        selected_count = artifact.get("selected_count")
        reference = artifact.get("ref", artifact.get("shortlist_id"))
        if (
            artifact.get("kind") == "shortlist"
            and isinstance(selected_count, int)
            and not isinstance(selected_count, bool)
            and selected_count == 0
            and isinstance(reference, str)
            and reference in payload.canonical_refs
        ):
            return reference
    return None


def _validate_zero_selection_publication(
    connection: sqlite3.Connection,
    payload: OperationPayload,
    *,
    as_of: date,
) -> None:
    reference = _zero_selection_reference(payload)
    if reference is None:  # structural validation reports the field-level error
        return
    canonical = connection.execute(
        """
        SELECT shortlist_id FROM shortlist
        WHERE as_of = ?
        ORDER BY published_at DESC, shortlist_id DESC
        LIMIT 1
        """,
        (as_of.isoformat(),),
    ).fetchone()
    if canonical is None or str(canonical["shortlist_id"]) != reference:
        raise OperationCompletionError(
            f"opportunity completion requires the canonical shortlist at {as_of}: {reference}"
        )
    row = connection.execute(
        "SELECT as_of, payload FROM shortlist WHERE shortlist_id = ?",
        (reference,),
    ).fetchone()
    if row is None:
        raise OperationCompletionError(
            f"opportunity completion requires published shortlist: {reference}"
        )
    document = json.loads(str(row["payload"]))
    entries = document.get("entries") if isinstance(document, dict) else None
    if (
        str(row["as_of"]) != as_of.isoformat()
        or not isinstance(document, dict)
        or document.get("kind") != "shortlist"
        or document.get("shortlist_id") != reference
        or document.get("as_of") != as_of.isoformat()
    ):
        raise OperationCompletionError(
            f"opportunity completion requires {reference} at session as-of {as_of}"
        )
    if (
        not isinstance(entries, list)
        or not entries
        or any(
            not isinstance(entry, dict)
            or entry.get("decision") not in {"selected", "rejected"}
            or entry.get("decision") == "selected"
            for entry in entries
        )
    ):
        raise OperationCompletionError(
            f"opportunity completion requires zero selected entries in {reference}"
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
