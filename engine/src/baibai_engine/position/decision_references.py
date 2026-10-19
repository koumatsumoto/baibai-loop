"""買付の報告先を識別する最小参照と、明示的な取引事実の binding。"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from baibai_engine.collection.identifiers import normalize_ticker
from baibai_engine.position.ledger import ExecutionEvent, LedgerEvent, ReservationEvent
from baibai_engine.position.ledger_read import LedgerConflictError
from baibai_engine.storage.application.paths import database_path
from baibai_engine.storage.application.read import connect_read_only


class DecisionReferenceBinding(BaseModel):
    """初回 direct-fact draft に固定し、ledger と同時に保存する参照。"""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    reference: Annotated[
        str,
        Field(
            pattern=r"^manual-fact-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    ]
    ticker: Annotated[str, Field(pattern=r"^[0-9A-Z]{4}$")]
    side: Literal["buy"] = "buy"
    origin: Literal["manual_fact"] = "manual_fact"
    proposal_id: None = None
    source_id: str
    source_digest: None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _ticker(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("ticker must be text")
        return normalize_ticker(value)

    @model_validator(mode="after")
    def _source(self) -> DecisionReferenceBinding:
        if self.source_id != self.reference:
            raise ValueError("manual-fact source_id must equal its reference")
        return self


def resolve_buy_reference(reference: str, *, db_path: Path | None = None) -> str:
    """保存 binding の ticker だけを読む。現在の提案適格性は再評価しない。"""
    if not database_path(db_path).is_file():
        raise ValueError("application database is unavailable")
    with closing(connect_read_only(db_path)) as connection:
        row = connection.execute(
            "SELECT ticker, side FROM position_decision_reference WHERE reference = ?",
            (reference,),
        ).fetchone()
    if row is None:
        raise ValueError(f"buy decision reference is unavailable: {reference}")
    if row[1] != "buy":
        raise ValueError("decision reference does not identify a buy")
    ticker = normalize_ticker(row[0])
    if ticker != row[0] or len(ticker) != 4:
        raise ValueError("buy decision reference ticker is invalid")
    return ticker


def add_manual_fact_binding(
    connection: sqlite3.Connection,
    binding: DecisionReferenceBinding,
    *,
    additions: Sequence[LedgerEvent],
) -> None:
    """呼出元の CAS transaction 内だけで追加する。独立 commit はしない。"""
    referenced = tuple(
        event
        for event in additions
        if getattr(event, "decision_reference", None) == binding.reference
    )
    if not referenced:
        raise LedgerConflictError("manual-fact binding requires a new buy event")
    if any(
        not isinstance(event, (ExecutionEvent, ReservationEvent))
        or event.ticker != binding.ticker
        or event.side != binding.side
        for event in referenced
    ):
        raise LedgerConflictError("all manual-fact events must match binding ticker and side")
    values = (
        binding.reference,
        binding.ticker,
        binding.side,
        binding.origin,
        binding.proposal_id,
        binding.source_id,
        binding.source_digest,
    )
    existing = connection.execute(
        """SELECT reference, ticker, side, origin, proposal_id, source_id, source_digest
           FROM position_decision_reference WHERE reference = ?""",
        (binding.reference,),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != values:
            raise LedgerConflictError("decision reference binding conflicts")
        return
    connection.execute(
        """INSERT INTO position_decision_reference
           (reference, ticker, side, origin, proposal_id, source_id, source_digest)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        values,
    )
