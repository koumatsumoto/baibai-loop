from __future__ import annotations

import copy
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.ledger import (
    PortfolioLedgerDocument,
    PortfolioSnapshot,
    reconcile_portfolio,
)
from baibai_engine.proposals.store import (
    ProposalConflictError,
    ProposalStoreService,
    ProposalValidationError,
)
from baibai_engine.research.execution_policy import ExecutionPolicyInput
from baibai_engine.research.store import ResearchStoreService

ROOT = Path(__file__).parents[1]
PACKET = ROOT / "tests/fixtures/decision-packet/2331-decision.yaml"
REVIEW = ROOT / "tests/fixtures/decision-packet/2331-decision-review.yaml"
POLICY = ROOT / "tests/fixtures/execution-policy/current-ladder.yaml"
LEDGER = ROOT / "tests/fixtures/portfolio-ledger/representative.yaml"
PACKET_ID = "packet-20260711-2331-r1"
CREATED_AT = datetime.fromisoformat("2026-07-11T10:02:00+09:00")


def _raw(path: Path) -> dict[str, object]:
    parsed = safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return copy.deepcopy(parsed)


def _database(path: Path, *, with_review: bool = True) -> None:
    service = ResearchStoreService(path)
    if with_review:
        service.publish_packet_with_review(PACKET_ID, _raw(PACKET), _raw(REVIEW))
    else:
        packet = _raw(PACKET)
        packet["judgment"]["recommendation"] = "defer"  # type: ignore[index]
        packet["judgment"]["sizing_action"] = "none"  # type: ignore[index]
        service.publish_packet(PACKET_ID, packet)


def _policy() -> ExecutionPolicyInput:
    return ExecutionPolicyInput.model_validate(_raw(POLICY))


def _snapshot() -> PortfolioSnapshot:
    return reconcile_portfolio(PortfolioLedgerDocument.model_validate(_raw(LEDGER)))


def test_create_uses_db_packet_review_and_replaces_caller_portfolio(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    raw = _raw(POLICY)
    portfolio = raw["portfolio"]
    assert isinstance(portfolio, dict)
    portfolio["available_cash_yen"] = 1
    portfolio["board_lot"] = 1
    portfolio["adv_participation_warning_pct"] = 99.0
    proposal = ProposalStoreService(path).create(
        PACKET_ID,
        ExecutionPolicyInput.model_validate(raw),
        _snapshot(),
        created_at=CREATED_AT,
    )

    assert proposal.proposal_id == "prop-20260711-2331-1"
    assert proposal.status == "pending"
    assert proposal.packet_id == PACKET_ID
    assert proposal.review_id == "review-2331-20260703"
    stored_input = proposal.payload["execution_input"]
    assert isinstance(stored_input, dict)
    stored_portfolio = stored_input["portfolio"]
    assert isinstance(stored_portfolio, dict)
    assert stored_portfolio["available_cash_yen"] == _snapshot().available_cash_yen
    assert stored_portfolio["board_lot"] == 100
    assert stored_portfolio["adv_participation_warning_pct"] == 5.0
    generated = proposal.payload["execution_proposal"]
    assert isinstance(generated, dict)
    assert generated["recommended_tactic"] == "buy_now"
    assert generated["orders"]


def test_internal_ids_are_allocated_without_collision_under_write_lock(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = ProposalStoreService(path)
    first = service.create(PACKET_ID, _policy(), _snapshot(), created_at=CREATED_AT)
    second = service.create(PACKET_ID, _policy(), _snapshot(), created_at=CREATED_AT)

    assert first.proposal_id == "prop-20260711-2331-1"
    assert second.proposal_id == "prop-20260711-2331-2"


def test_create_requires_one_matching_ready_buy_review_without_write(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path, with_review=False)

    with pytest.raises(ProposalValidationError, match="exactly one review"):
        ProposalStoreService(path).create(
            PACKET_ID,
            _policy(),
            _snapshot(),
            created_at=CREATED_AT,
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM proposal").fetchone()[0] == 0


def test_human_decision_can_move_between_non_approved_current_states(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = ProposalStoreService(path)
    proposal = service.create(PACKET_ID, _policy(), _snapshot(), created_at=CREATED_AT)

    deferred = service.decide(
        proposal.proposal_id,
        "defer",
        decided_at=CREATED_AT + timedelta(minutes=1),
    )
    rejected = service.decide(
        proposal.proposal_id,
        "reject",
        decided_at=CREATED_AT + timedelta(minutes=2),
    )

    assert deferred.status == "deferred"
    assert rejected.status == "rejected"
    assert rejected.decided_at == CREATED_AT + timedelta(minutes=2)


def test_decision_time_cannot_move_backwards(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = ProposalStoreService(path)
    proposal = service.create(PACKET_ID, _policy(), _snapshot(), created_at=CREATED_AT)
    service.decide(
        proposal.proposal_id,
        "defer",
        decided_at=CREATED_AT + timedelta(minutes=2),
    )

    with pytest.raises(ProposalValidationError, match="cannot predate"):
        service.decide(
            proposal.proposal_id,
            "reject",
            decided_at=CREATED_AT + timedelta(minutes=1),
        )

    current = service.get(proposal.proposal_id)
    assert current.status == "deferred"
    assert current.decided_at == CREATED_AT + timedelta(minutes=2)


def test_approve_recalculates_and_accepts_an_unchanged_current_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = ProposalStoreService(path)
    proposal = service.create(PACKET_ID, _policy(), _snapshot(), created_at=CREATED_AT)

    approved = service.decide(
        proposal.proposal_id,
        "approve",
        decided_at=CREATED_AT + timedelta(minutes=1),
        snapshot=_snapshot(),
    )

    assert approved.status == "approved"


def test_approve_rejects_planning_limit_drift_without_write(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = ProposalStoreService(path)
    proposal = service.create(PACKET_ID, _policy(), _snapshot(), created_at=CREATED_AT)
    changed = replace(_snapshot(), available_cash_yen=_snapshot().available_cash_yen - 100_000)

    with pytest.raises(ProposalConflictError, match="create a new proposal"):
        service.decide(
            proposal.proposal_id,
            "approve",
            decided_at=CREATED_AT + timedelta(minutes=1),
            snapshot=changed,
        )

    assert service.get(proposal.proposal_id).status == "pending"


def test_approve_requires_current_input_and_snapshot_without_write(tmp_path: Path) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = ProposalStoreService(path)
    proposal = service.create(PACKET_ID, _policy(), _snapshot(), created_at=CREATED_AT)

    with pytest.raises(ProposalValidationError, match="current DB-derived"):
        service.decide(
            proposal.proposal_id,
            "approve",
            decided_at=CREATED_AT + timedelta(minutes=1),
        )
    with pytest.raises(ProposalValidationError, match="current"):
        service.decide(
            proposal.proposal_id,
            "approve",
            decided_at=CREATED_AT + timedelta(minutes=10),
            snapshot=_snapshot(),
        )

    assert service.get(proposal.proposal_id).status == "pending"


def test_ledger_reference_locks_decision_and_approved_is_not_redecidable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.sqlite"
    _database(path)
    service = ProposalStoreService(path)
    first = service.create(PACKET_ID, _policy(), _snapshot(), created_at=CREATED_AT)
    service.decide(
        first.proposal_id,
        "approve",
        decided_at=CREATED_AT + timedelta(minutes=1),
        snapshot=_snapshot(),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO ledger_event(
                event_id, occurred_at, same_instant_order, event_type,
                proposal_id, payload
            ) VALUES ('event-1', ?, 0, 'contribution', ?, '{}')
            """,
            ((CREATED_AT + timedelta(minutes=1)).isoformat(), first.proposal_id),
        )

    with pytest.raises(ProposalConflictError, match="ledger event"):
        service.decide(
            first.proposal_id,
            "reject",
            decided_at=CREATED_AT + timedelta(minutes=2),
        )
    assert service.get(first.proposal_id).status == "approved"

    second = service.create(PACKET_ID, _policy(), _snapshot(), created_at=CREATED_AT)
    service.decide(
        second.proposal_id,
        "approve",
        decided_at=CREATED_AT + timedelta(minutes=1),
        snapshot=_snapshot(),
    )
    with pytest.raises(ProposalConflictError, match="cannot be re-decided"):
        service.decide(
            second.proposal_id,
            "defer",
            decided_at=CREATED_AT + timedelta(minutes=2),
        )
