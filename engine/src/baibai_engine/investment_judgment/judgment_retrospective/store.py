"""判断振り返りのimmutable追記保存。現在の企業研究headや注文資格を変更しない。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from baibai_engine.investment_judgment.contracts import _datetime
from baibai_engine.investment_judgment.judgment_retrospective.document import (
    RetrospectiveDocument,
    RetrospectiveDraft,
    RetrospectivePayload,
    RetrospectiveRecord,
    RetrospectiveRef,
    retrospective_sha256,
)
from baibai_engine.primitives.json import canonical_json
from baibai_engine.storage.sqlite.application.write import connect_rw, initialize_database


class RetrospectiveConflictError(ValueError):
    """同IDを別内容で保存することはできない。訂正も新IDで追記する。"""


class RetrospectiveReadError(ValueError):
    """指定された保存行またはexact参照が不正。最新へのfallbackはしない。"""


def load_retrospective(
    connection: sqlite3.Connection, retrospective_id: str
) -> RetrospectiveRecord:
    row = connection.execute(
        "SELECT ticker,as_of,recorded_at,payload,payload_sha256 "
        "FROM judgment_retrospective WHERE retrospective_id=?",
        (retrospective_id,),
    ).fetchone()
    if row is None:
        raise RetrospectiveReadError(f"unknown Retrospective: {retrospective_id}")
    try:
        payload = RetrospectivePayload.model_validate_json(str(row[3]))
        if (str(row[0]), str(row[1])) != (
            payload.document.ticker,
            payload.document.as_of.isoformat(),
        ):
            raise ValueError("Retrospective metadata differs from payload")
        return RetrospectiveRecord(
            reference=RetrospectiveRef(
                retrospective_id=retrospective_id, payload_sha256=str(row[4])
            ),
            recorded_at=_datetime(str(row[2])),
            document=payload.document,
            ledger=payload.ledger,
        )
    except ValueError as error:
        raise RetrospectiveReadError(
            f"unreadable Retrospective {retrospective_id}: {error}"
        ) from error


def require_retrospective_ref(
    connection: sqlite3.Connection, reference: RetrospectiveRef
) -> RetrospectiveRecord:
    record = load_retrospective(connection, reference.retrospective_id)
    if record.reference != reference:
        raise RetrospectiveReadError("Retrospective reference differs from stored content")
    return record


def validate_correction(
    connection: sqlite3.Connection, document: RetrospectiveDocument, *, recorded_at: datetime
) -> None:
    if document.corrects_ref is None:
        return
    prior = require_retrospective_ref(connection, document.corrects_ref)
    if prior.document.ticker != document.ticker or prior.document.origin != document.origin:
        raise ValueError("correction must refer to the same ticker and origin")
    if prior.recorded_at > recorded_at:
        raise ValueError("corrected Retrospective is after recorded_at")


class RetrospectiveStoreService:
    def __init__(
        self, db_path: Path | None = None, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._db_path = db_path
        self._clock = clock or (lambda: datetime.now(UTC))

    def save(self, draft: RetrospectiveDraft) -> RetrospectiveRecord:
        draft = RetrospectiveDraft.model_validate(draft.model_dump(mode="python"))
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = self._save(connection, draft)
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def _save(
        self, connection: sqlite3.Connection, draft: RetrospectiveDraft
    ) -> RetrospectiveRecord:
        from baibai_engine.investment_judgment.judgment_retrospective.query import (
            capture_ledger_snapshot,
            retrospective_context,
        )

        existing = connection.execute(
            "SELECT 1 FROM judgment_retrospective WHERE retrospective_id=?",
            (draft.retrospective_id,),
        ).fetchone()
        if existing is not None:
            record = load_retrospective(connection, draft.retrospective_id)
            saved_head = None if record.ledger is None else record.ledger.append_head
            if record.document != draft.document or saved_head != draft.ledger_append_head:
                raise RetrospectiveConflictError(
                    f"immutable Retrospective differs: {draft.retrospective_id}"
                )
            return record
        now = _datetime(self._clock()).astimezone(UTC)
        ledger = capture_ledger_snapshot(
            connection, draft.document, append_head=draft.ledger_append_head
        )
        payload = RetrospectivePayload(document=draft.document, ledger=ledger)
        record = RetrospectiveRecord(
            reference=RetrospectiveRef(
                retrospective_id=draft.retrospective_id,
                payload_sha256=retrospective_sha256(payload),
            ),
            recorded_at=now,
            document=payload.document,
            ledger=payload.ledger,
        )
        retrospective_context(connection, record)
        validate_correction(connection, record.document, recorded_at=now)
        connection.execute(
            "INSERT INTO judgment_retrospective(retrospective_id,ticker,as_of,recorded_at,"
            "payload,payload_sha256) VALUES (?,?,?,?,?,?)",
            (
                draft.retrospective_id,
                record.document.ticker,
                record.document.as_of.isoformat(),
                now.isoformat(),
                canonical_json(payload.model_dump(mode="json")),
                record.reference.payload_sha256,
            ),
        )
        return record
