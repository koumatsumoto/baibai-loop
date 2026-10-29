"""自己完結した企業調査snapshotの追記保存とexact読取。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.investment_judgment.company_research.document import (
    ResearchDocument,
    ResearchDraft,
    ResearchRecord,
    ResearchRef,
    research_sha256,
)
from baibai_engine.investment_judgment.contracts import _datetime
from baibai_engine.primitives.json import canonical_json
from baibai_engine.primitives.time import JST
from baibai_engine.storage.sqlite.application.write import connect_rw, initialize_database


class ResearchConflictError(ValueError):
    """同IDの別内容または既に更新されたpriorからの保存。"""


class ResearchReadError(ValueError):
    """選ばれたResearch行が不正。古い正常行へは戻さない。"""


def latest_research_id(
    connection: sqlite3.Connection, ticker: str, *, as_of: date, known_at: datetime
) -> str | None:
    _datetime(known_at)
    rows = connection.execute(
        "SELECT research_id, as_of, recorded_at FROM company_research WHERE ticker=?", (ticker,)
    ).fetchall()
    headers: list[tuple[date, datetime, str]] = []
    for row in rows:
        try:
            information_date = date.fromisoformat(str(row[1]))
            recorded_at = _datetime(str(row[2]))
        except ValueError as error:
            raise ResearchReadError(f"invalid Research revision header: {row[0]}") from error
        if information_date <= as_of and recorded_at <= known_at:
            headers.append((information_date, recorded_at, str(row[0])))
    return max(headers)[2] if headers else None


def load_research(connection: sqlite3.Connection, research_id: str) -> ResearchRecord:
    row = connection.execute(
        "SELECT ticker, as_of, recorded_at, prior_research_id, payload, payload_sha256 "
        "FROM company_research WHERE research_id=?",
        (research_id,),
    ).fetchone()
    if row is None:
        raise ResearchReadError(f"unknown Research: {research_id}")
    try:
        document = ResearchDocument.model_validate_json(str(row[4]))
        if (str(row[0]), str(row[1])) != (document.ticker, document.as_of.isoformat()):
            raise ValueError("Research metadata differs from payload")
        prior_ref = None
        if row[3] is not None:
            prior = connection.execute(
                "SELECT ticker, as_of, recorded_at, payload_sha256 FROM company_research "
                "WHERE research_id=?",
                (row[3],),
            ).fetchone()
            if prior is None or prior[0] != document.ticker:
                raise ValueError("Research prior is missing or belongs to another ticker")
            if (
                (document.as_of, _datetime(str(row[2])), research_id)
                <= (date.fromisoformat(str(prior[1])), _datetime(str(prior[2])), str(row[3]))
                or document.as_of < date.fromisoformat(str(prior[1]))
                or _datetime(str(row[2])) < _datetime(str(prior[2]))
            ):
                raise ValueError("Research revision order does not advance")
            prior_ref = ResearchRef(research_id=str(row[3]), payload_sha256=str(prior[3]))
        return ResearchRecord(
            reference=ResearchRef(research_id=research_id, payload_sha256=str(row[5])),
            recorded_at=_datetime(str(row[2])),
            prior_research_ref=prior_ref,
            document=document,
        )
    except ValueError as error:
        raise ResearchReadError(f"unreadable Research {research_id}: {error}") from error


def require_research_ref(connection: sqlite3.Connection, reference: ResearchRef) -> ResearchRecord:
    record = load_research(connection, reference.research_id)
    if record.reference != reference:
        raise ResearchReadError("Research reference differs from stored content")
    return record


class ResearchStoreService:
    def __init__(
        self, db_path: Path | None = None, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._db_path = db_path
        self._clock = clock or (lambda: datetime.now(JST))

    def save(self, draft: ResearchDraft) -> ResearchRecord:
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

    def _save(self, connection: sqlite3.Connection, draft: ResearchDraft) -> ResearchRecord:
        payload = canonical_json(draft.document.model_dump(mode="json"))
        existing = connection.execute(
            "SELECT payload FROM company_research WHERE research_id=?", (draft.research_id,)
        ).fetchone()
        if existing is not None:
            record = load_research(connection, draft.research_id)
            if str(existing[0]) != payload or record.prior_research_ref != draft.prior_research_ref:
                raise ResearchConflictError(f"immutable Research differs: {draft.research_id}")
            return record
        now = _datetime(self._clock())
        # An unbounded header selection enforces one chain, including records after a skewed clock.
        latest = latest_research_id(
            connection,
            draft.document.ticker,
            as_of=date.max,
            known_at=datetime.max.replace(tzinfo=JST),
        )
        prior = None if latest is None else load_research(connection, latest)
        if (None if prior is None else prior.reference) != draft.prior_research_ref:
            raise ResearchConflictError(f"new Research must follow latest: {latest}")
        if prior is not None and (
            draft.document.as_of < prior.document.as_of
            or now < prior.recorded_at
            or (draft.document.as_of, now, draft.research_id)
            <= (prior.document.as_of, prior.recorded_at, prior.reference.research_id)
        ):
            raise ResearchConflictError("new Research must advance as_of/time/revision order")
        record = ResearchRecord(
            reference=ResearchRef(
                research_id=draft.research_id, payload_sha256=research_sha256(draft.document)
            ),
            recorded_at=now,
            prior_research_ref=draft.prior_research_ref,
            document=draft.document,
        )
        connection.execute(
            "INSERT INTO company_research(research_id,ticker,as_of,recorded_at,prior_research_id,"
            "payload,payload_sha256) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                draft.research_id,
                draft.document.ticker,
                draft.document.as_of.isoformat(),
                now.isoformat(),
                latest,
                payload,
                record.reference.payload_sha256,
            ),
        )
        return record
