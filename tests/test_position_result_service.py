from __future__ import annotations

import copy
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.position.cli import main
from baibai_engine.position.drafts import apply_draft, load_draft
from baibai_engine.position.ledger import load_portfolio_ledger, reconcile_portfolio
from baibai_engine.position.result_service import build_result_draft
from baibai_engine.position.store import LedgerStoreService
from baibai_engine.proposals.store import ProposalStoreService
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
    value = safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return copy.deepcopy(value)


def _approved(tmp_path: Path) -> tuple[LedgerStoreService, ProposalStoreService, str]:
    db = tmp_path / "app.sqlite"
    ResearchStoreService(db).publish_packet_with_review(PACKET_ID, _raw(PACKET), _raw(REVIEW))
    ledger = LedgerStoreService(db)
    source = load_portfolio_ledger(LEDGER)
    ledger.import_document(
        source.model_copy(
            update={
                "market_prices": tuple(
                    price.model_copy(update={"source_kind": "licensed_dataset"})
                    for price in source.market_prices
                )
            }
        )
    )
    proposals = ProposalStoreService(db)
    snapshot = reconcile_portfolio(ledger.load())
    proposal = proposals.create(
        PACKET_ID,
        ExecutionPolicyInput.model_validate(_raw(POLICY)),
        snapshot,
        created_at=CREATED_AT,
    )
    proposals.decide(
        proposal.proposal_id,
        "approve",
        decided_at=CREATED_AT + timedelta(minutes=1),
        snapshot=snapshot,
    )
    return ledger, proposals, proposal.proposal_id


def test_approved_proposal_builds_bound_open_draft(tmp_path: Path) -> None:
    ledger, proposals, proposal_id = _approved(tmp_path)
    proposal = proposals.get(proposal_id)
    generated = proposal.payload["execution_proposal"]
    assert isinstance(generated, dict)
    orders = generated["orders"]
    assert isinstance(orders, list)
    assert orders
    order = orders[0]
    assert isinstance(order, dict)

    draft, event_ids = build_result_draft(
        ledger,
        proposals,
        proposal_id=proposal_id,
        status="open",
        occurred_at=CREATED_AT + timedelta(minutes=2),
        ticker="2331",
        quantity=int(str(order["quantity"])),
        sector="サービス業",
        price_guard_yen=Decimal(str(order["limit_price_yen"])),
        expires_at=datetime.fromisoformat(str(order["expires_at"])),
        now=CREATED_AT + timedelta(minutes=3),
    )

    assert draft is not None
    assert event_ids
    applied = apply_draft(ledger, draft, human_confirmed=True)
    assert applied.event_ids == event_ids
    with sqlite3.connect(tmp_path / "app.sqlite") as connection:
        rows = connection.execute(
            "SELECT proposal_id FROM ledger_event WHERE proposal_id = ?", (proposal_id,)
        ).fetchall()
    assert rows == [(proposal_id,)]


def test_unapproved_and_order_drift_do_not_build_result(tmp_path: Path) -> None:
    ledger, proposals, proposal_id = _approved(tmp_path)
    with pytest.raises(ValueError, match="does not match approved proposal"):
        build_result_draft(
            ledger,
            proposals,
            proposal_id=proposal_id,
            status="open",
            occurred_at=CREATED_AT + timedelta(minutes=2),
            ticker="2331",
            quantity=999,
            sector="サービス業",
            price_guard_yen=Decimal("1"),
            expires_at=CREATED_AT + timedelta(days=1),
            now=CREATED_AT + timedelta(minutes=3),
        )


def test_record_result_cli_builds_db_bound_draft_without_applying(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, proposals, proposal_id = _approved(tmp_path)
    proposal = proposals.get(proposal_id)
    generated = proposal.payload["execution_proposal"]
    assert isinstance(generated, dict)
    orders = generated["orders"]
    assert isinstance(orders, list)
    order = orders[0]
    assert isinstance(order, dict)
    before = ledger.load()

    assert (
        main(
            [
                "record-result",
                "--root",
                str(tmp_path),
                "--db",
                str(tmp_path / "app.sqlite"),
                "--proposal-ref",
                proposal_id,
                "--status",
                "open",
                "--occurred-at",
                (CREATED_AT + timedelta(minutes=2)).isoformat(),
                "--ticker",
                "2331",
                "--quantity",
                str(order["quantity"]),
                "--sector",
                "サービス業",
                "--price-guard-yen",
                str(order["limit_price_yen"]),
                "--expires-at",
                str(order["expires_at"]),
                "--out",
                "draft.yaml",
            ],
            now=CREATED_AT + timedelta(minutes=3),
        )
        == 0
    )

    output = yaml.safe_load(capsys.readouterr().out)
    assert output["proposal_id"] == proposal_id
    assert output["status"] == "draft_created"
    draft = load_draft(tmp_path / "draft.yaml")
    assert draft.expected_head == ledger.append_head()
    assert ledger.load() == before
