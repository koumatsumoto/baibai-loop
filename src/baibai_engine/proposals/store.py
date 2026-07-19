"""Current-state trade proposals with fail-closed planning-limit validation."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import ValidationError

from baibai_engine.appdb.json import canonical_json
from baibai_engine.appdb.write import connect_rw, initialize_database
from baibai_engine.position.ledger import PortfolioSnapshot
from baibai_engine.research.decision_packet import (
    DecisionPacketDocument,
    IndependentReview,
    evaluate_decision_packet,
)
from baibai_engine.research.execution_policy import (
    ExecutionPolicyError,
    ExecutionPolicyInput,
    evaluate_execution_policy,
    execution_proposal_to_payload,
    portfolio_input_from_snapshot,
    require_current_execution_input,
)

ProposalStatus = Literal["pending", "approved", "deferred", "rejected"]
ProposalDecision = Literal["approve", "defer", "reject"]


class ProposalValidationError(ValueError):
    """A proposal input does not satisfy the current domain contract."""


class ProposalConflictError(ValueError):
    """A proposal decision conflicts with current canonical state."""


class ProposalNotFoundError(ValueError):
    """A requested proposal does not exist."""


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    proposal_id: str
    ticker: str
    packet_id: str
    review_id: str
    created_at: datetime
    status: ProposalStatus
    decided_at: datetime | None
    payload: Mapping[str, object]


class ProposalStoreService:
    """Create deterministic proposals and record only human-reported current decisions."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path

    def create(
        self,
        packet_id: str,
        execution_input: ExecutionPolicyInput,
        snapshot: PortfolioSnapshot,
        *,
        created_at: datetime,
    ) -> ProposalRecord:
        """Create one pending proposal from an immutable ready packet and current ledger."""
        _require_aware(created_at, "created_at")
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                packet, review = _ready_packet_and_review(connection, packet_id)
                effective_input = _input_with_snapshot(execution_input, snapshot)
                require_current_execution_input(effective_input, now=created_at)
                proposal = evaluate_execution_policy(
                    packet,
                    evaluate_decision_packet(packet, review=review),
                    effective_input,
                )
                proposal_id = _allocate_proposal_id(
                    connection,
                    ticker=proposal.ticker,
                    created_at=created_at,
                )
                payload: dict[str, object] = {
                    "packet_id": packet_id,
                    "review_id": review.review_id,
                    "execution_input": effective_input.model_dump(mode="python"),
                    "execution_proposal": execution_proposal_to_payload(proposal),
                }
                connection.execute(
                    """
                    INSERT INTO proposal (
                        proposal_id, ticker, packet_id, review_id, created_at,
                        status, decided_at, payload
                    ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?)
                    """,
                    (
                        proposal_id,
                        proposal.ticker,
                        packet_id,
                        review.review_id,
                        created_at.isoformat(),
                        canonical_json(payload),
                    ),
                )
                row = _proposal_row(connection, proposal_id)
                connection.commit()
            except (ExecutionPolicyError, ValidationError) as error:
                connection.rollback()
                raise ProposalValidationError(str(error)) from error
            except BaseException:
                connection.rollback()
                raise
        return _record(row)

    def decide(
        self,
        proposal_id: str,
        decision: ProposalDecision,
        *,
        decided_at: datetime,
        snapshot: PortfolioSnapshot | None = None,
    ) -> ProposalRecord:
        """Record a human report, revalidating an approval against current DB state."""
        _require_aware(decided_at, "decided_at")
        target = _decision_status(decision)
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = _proposal_row(connection, proposal_id)
                current = cast(ProposalStatus, str(row["status"]))
                created_at = datetime.fromisoformat(str(row["created_at"]))
                previous_decided_at = (
                    datetime.fromisoformat(str(row["decided_at"]))
                    if row["decided_at"] is not None
                    else None
                )
                if decided_at < created_at or (
                    previous_decided_at is not None and decided_at < previous_decided_at
                ):
                    raise ProposalValidationError(
                        "decided_at cannot predate proposal creation or its current decision"
                    )
                if _ledger_references_proposal(connection, proposal_id):
                    raise ProposalConflictError(
                        "proposal is immutable after a ledger event references it"
                    )
                if current == "approved":
                    raise ProposalConflictError("approved proposal cannot be re-decided")
                if target == "approved":
                    if snapshot is None:
                        raise ProposalValidationError(
                            "approval requires a current DB-derived portfolio snapshot"
                        )
                    _revalidate_approval(
                        connection,
                        row,
                        snapshot=snapshot,
                        decided_at=decided_at,
                    )
                connection.execute(
                    "UPDATE proposal SET status = ?, decided_at = ? WHERE proposal_id = ?",
                    (target, decided_at.isoformat(), proposal_id),
                )
                updated = _proposal_row(connection, proposal_id)
                connection.commit()
            except (ExecutionPolicyError, ValidationError) as error:
                connection.rollback()
                raise ProposalValidationError(str(error)) from error
            except BaseException:
                connection.rollback()
                raise
        return _record(updated)

    def get(self, proposal_id: str) -> ProposalRecord:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            return _record(_proposal_row(connection, proposal_id))

    def list(self, *, status: ProposalStatus | None = None) -> tuple[ProposalRecord, ...]:
        initialize_database(self._db_path)
        with closing(connect_rw(self._db_path)) as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM proposal ORDER BY created_at DESC, proposal_id DESC"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM proposal
                    WHERE status = ?
                    ORDER BY created_at DESC, proposal_id DESC
                    """,
                    (status,),
                ).fetchall()
        return tuple(_record(cast(sqlite3.Row, row)) for row in rows)


def _ready_packet_and_review(
    connection: sqlite3.Connection,
    packet_id: str,
    *,
    review_id: str | None = None,
) -> tuple[DecisionPacketDocument, IndependentReview]:
    packet_row = connection.execute(
        "SELECT payload FROM research_packet WHERE packet_id = ?",
        (packet_id,),
    ).fetchone()
    if packet_row is None:
        raise ProposalValidationError(f"unknown research packet: {packet_id}")
    packet = DecisionPacketDocument.model_validate_json(str(packet_row["payload"]))
    if review_id is None:
        review_rows = connection.execute(
            """
            SELECT review_id, payload FROM research_review
            WHERE packet_id = ?
            ORDER BY reviewed_at DESC, review_id DESC
            """,
            (packet_id,),
        ).fetchall()
        if len(review_rows) != 1:
            raise ProposalValidationError(
                f"proposal requires exactly one review for packet: {packet_id}"
            )
        review_row = review_rows[0]
    else:
        review_row = connection.execute(
            """
            SELECT review_id, payload FROM research_review
            WHERE packet_id = ? AND review_id = ?
            """,
            (packet_id, review_id),
        ).fetchone()
        if review_row is None:
            raise ProposalConflictError("proposal review no longer matches its packet")
    review = IndependentReview.model_validate_json(str(review_row["payload"]))
    result = evaluate_decision_packet(packet, review=review)
    if result.decision_readiness != "ready" or result.errors:
        detail = "; ".join(result.errors) or result.decision_readiness
        raise ProposalValidationError(f"research packet is not decision-ready: {detail}")
    if packet.judgment.recommendation != "buy":
        raise ProposalValidationError("proposal requires a ready buy research packet")
    return packet, review


def _input_with_snapshot(
    execution_input: ExecutionPolicyInput,
    snapshot: PortfolioSnapshot,
) -> ExecutionPolicyInput:
    portfolio = portfolio_input_from_snapshot(
        snapshot,
        ticker=execution_input.ticker,
        sector=execution_input.sector,
        common_factors=execution_input.common_factors,
        spread_warning_bps=execution_input.portfolio.spread_warning_bps,
    )
    return execution_input.model_copy(update={"portfolio": portfolio})


def _revalidate_approval(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    snapshot: PortfolioSnapshot,
    decided_at: datetime,
) -> None:
    payload = _payload(row)
    raw_input = payload.get("execution_input")
    saved_proposal = payload.get("execution_proposal")
    if not isinstance(raw_input, Mapping) or not isinstance(saved_proposal, Mapping):
        raise ProposalConflictError("stored proposal payload is incomplete")
    execution_input = ExecutionPolicyInput.model_validate(raw_input)
    effective_input = _input_with_snapshot(execution_input, snapshot)
    require_current_execution_input(effective_input, now=decided_at)
    packet, review = _ready_packet_and_review(
        connection,
        str(row["packet_id"]),
        review_id=str(row["review_id"]),
    )
    regenerated = execution_proposal_to_payload(
        evaluate_execution_policy(
            packet,
            evaluate_decision_packet(packet, review=review),
            effective_input,
        )
    )
    if canonical_json(regenerated) != canonical_json(saved_proposal):
        raise ProposalConflictError("current planning-limit result differs; create a new proposal")


def _allocate_proposal_id(
    connection: sqlite3.Connection,
    *,
    ticker: str,
    created_at: datetime,
) -> str:
    prefix = f"prop-{created_at.date().strftime('%Y%m%d')}-{ticker}-"
    rows = connection.execute(
        "SELECT proposal_id FROM proposal WHERE proposal_id LIKE ?",
        (f"{prefix}%",),
    ).fetchall()
    suffixes = [
        int(suffix) for row in rows if (suffix := str(row["proposal_id"])[len(prefix) :]).isdigit()
    ]
    return f"{prefix}{max(suffixes, default=0) + 1}"


def _ledger_references_proposal(connection: sqlite3.Connection, proposal_id: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM ledger_event WHERE proposal_id = ? LIMIT 1",
            (proposal_id,),
        ).fetchone()
        is not None
    )


def _proposal_row(connection: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM proposal WHERE proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    if row is None:
        raise ProposalNotFoundError(f"unknown proposal: {proposal_id}")
    return cast(sqlite3.Row, row)


def _record(row: sqlite3.Row) -> ProposalRecord:
    decided_at = str(row["decided_at"]) if row["decided_at"] is not None else None
    return ProposalRecord(
        proposal_id=str(row["proposal_id"]),
        ticker=str(row["ticker"]),
        packet_id=str(row["packet_id"]),
        review_id=str(row["review_id"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        status=cast(ProposalStatus, str(row["status"])),
        decided_at=datetime.fromisoformat(decided_at) if decided_at is not None else None,
        payload=_payload(row),
    )


def _payload(row: sqlite3.Row) -> Mapping[str, object]:
    parsed = json.loads(str(row["payload"]))
    if not isinstance(parsed, dict):
        raise ProposalConflictError("stored proposal payload must be an object")
    return cast(Mapping[str, object], parsed)


def _decision_status(decision: ProposalDecision) -> ProposalStatus:
    return cast(
        ProposalStatus,
        {
            "approve": "approved",
            "defer": "deferred",
            "reject": "rejected",
        }[decision],
    )


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProposalValidationError(f"{field} must include a timezone")
