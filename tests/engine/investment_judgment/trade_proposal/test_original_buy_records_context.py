"""買付報告の元提案に付いた別記録。合成の正規writerだけで検証する。"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from tests.helpers.generated_selection import seed_generated_selection
from tests.helpers.proposal import (
    PROPOSAL_ID,
    PUBLISHED,
    backend_case,
    read_saved_proposal,
    reviewed_proposal,
)

from baibai_engine.investment_judgment.trade_proposal import inputs as input_reader
from baibai_engine.investment_judgment.trade_proposal.contracts import proposal_draft_sha256
from baibai_engine.investment_judgment.trade_proposal.record_contracts import (
    HumanProposalReportDraft,
    ProposalCorrectionDraft,
    ProposalReference,
)
from baibai_engine.investment_judgment.trade_proposal.records import ProposalRecordService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.investment_judgment.workflow.operation.models import OperationPayload
from baibai_engine.investment_judgment.workflow.operation.service import OperationService
from baibai_engine.portfolio.ledger.events import ExecutionEvent, ReservationEvent
from baibai_engine.portfolio.ledger.store import LedgerStoreService

START = PUBLISHED + timedelta(minutes=20)
RECEIPT = PUBLISHED + timedelta(hours=1)
NOW = PUBLISHED + timedelta(hours=2)
B = "investment-proposal-00000000-0000-4000-8000-000000000004"


def _ref(proposal):
    return ProposalReference(
        proposal_id=proposal.proposal_id, draft_sha256=proposal_draft_sha256(proposal)
    )


def _setup(tmp_path, *, scope="review_positions", linked="erroneous", before_proposal=False):
    case = backend_case(tmp_path)
    a = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        reviewed_proposal(case)
    )
    b = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish(reviewed_proposal(case, proposal_id=B))
    case.operations.record_proposal(
        case.operation.operation_id, b.proposal_id, now=PUBLISHED + timedelta(minutes=2)
    )
    case.operations.complete(
        case.operation.operation_id,
        result="合成の初回提案を保存",
        next="報告された保有の再検討は別Operation",
        completed_at=PUBLISHED + timedelta(minutes=2),
    )
    reference = {"erroneous": b.proposal_id, "replacement": a.proposal_id, "unlinked": None}[linked]
    store = LedgerStoreService(case.app)
    ledger, head = store.load_with_head()
    bought_at = PUBLISHED + (timedelta(seconds=20) if before_proposal else timedelta(minutes=4))
    reservation = ReservationEvent.model_validate(
        dict(
            type="reservation",
            event_id="synthetic-order",
            reservation_id="synthetic-reservation",
            order_id="synthetic-broker-order",
            occurred_at=bought_at - timedelta(minutes=1),
            ticker="1234",
            side="buy",
            sector="情報・通信業",
            common_factors=["domestic-demand"],
            decision_reference=reference,
            quantity=100,
            price_guard_yen="1000",
            expires_at=NOW,
        )
    )
    execution = ExecutionEvent.model_validate(
        dict(
            type="execution",
            event_id="synthetic-fill",
            execution_id="synthetic-broker-fill",
            occurred_at=bought_at,
            ticker="1234",
            side="buy",
            reservation_id=reservation.reservation_id,
            decision_reference=reference,
            quantity=100,
            price_yen="1000",
        )
    )
    store.apply_document(
        expected_head=head,
        expected_document=ledger,
        replacement=ledger.model_copy(
            update={
                "events": (*ledger.events, reservation, execution),
                "as_of": PUBLISHED + timedelta(minutes=5),
            }
        ),
    )
    selected = seed_generated_selection(
        db=case.app,
        runs=case.runs,
        now=START,
        tickers=("1234",) if scope == "review_positions" else ("5678",),
        scope=scope,
    )
    operations = OperationService(case.app, runs_path=case.runs)
    operation = operations.start(
        session_kind="position-review" if scope == "review_positions" else "capital-allocation",
        as_of=selected.research_as_of,
        started_at=START,
        ticker="1234" if scope == "review_positions" else None,
        payload=OperationPayload(
            checkpoint="research", artifacts=(selected.model_dump(mode="json"),)
        ),
    )
    if scope == "review_positions":
        operations.record_assessment(operation.operation_id, case.pair.assessment_id, now=START)
    else:
        operations.record_unfinished(
            operation.operation_id,
            ticker="5678",
            reason="合成の未調査",
            references=("synthetic",),
            now=START,
        )
    return SimpleNamespace(case=case, a=a, b=b, operation=operation, store=store)


def _context(fixture, *, now=NOW):
    return input_reader.build_proposal_context(
        proposal_id=PROPOSAL_ID,
        operation_id=fixture.operation.operation_id,
        db_path=fixture.case.app,
        sqlite_path=fixture.case.market,
        now=now,
    )


def _correct(fixture):
    return ProposalRecordService(fixture.case.app, clock=lambda: RECEIPT).correct(
        ProposalCorrectionDraft(
            correction_id="synthetic-correction",
            erroneous_ref=_ref(fixture.b),
            replacement_ref=_ref(fixture.a),
            reason="合成：提案全体の誤記録を訂正。実施報告や各仮説の採否は変更しない。",
        ),
        expected_latest_ref=_ref(fixture.b),
    )


def test_original_buy_correction_is_separate_from_saved_basis_and_executed_position(tmp_path):
    f = _setup(tmp_path)
    before = _context(f)
    correction = _correct(f)
    snapshot = f.case.app.read_bytes()
    context = _context(f)
    supplemental = context.payload()["reported_buy_proposal_records"]
    assert supplemental["role"] == "reported_buy_reference_records_not_current_judgment"
    assert supplemental["recorded_through"] == NOW.isoformat()
    [row] = supplemental["holdings"]
    assert row["ticker"] == "1234"
    assert row["proposal_ref"] == _ref(f.b).model_dump(mode="json")
    assert row["decision_reference"] == f.b.proposal_id
    assert row["scope"] == "whole_proposal"
    assert row["status"] == "available"
    assert row["records"] == {
        "corrections": [correction.model_dump(mode="json")],
        "human_reports": [],
    }
    assert context.inputs.positions == before.inputs.positions
    assert context.inputs.positions[0].quantity == 100
    assert context.inputs.positions[0].original_assessment_ref == f.b.items[0].assessment_ref
    row["records"]["corrections"].clear()
    assert (
        len(
            context.payload()["reported_buy_proposal_records"]["holdings"][0]["records"][
                "corrections"
            ]
        )
        == 1
    )
    assert read_saved_proposal(proposal_id=f.b.proposal_id, db_path=f.case.app) == f.b
    retry = TradeProposalService(f.case.app, sqlite_path=f.case.market, clock=lambda: NOW).publish(
        f.b
    )
    assert retry == f.b
    assert f.case.app.read_bytes() == snapshot


def test_reported_reference_survives_when_the_proposal_was_not_known_at_the_buy(tmp_path):
    f = _setup(tmp_path, before_proposal=True)
    _correct(f)
    context = _context(f)
    [position] = context.inputs.positions
    assert position.original_assessment_ref is None
    assert "提案公開は買付より後" in position.original_reason_unavailable_reason
    [row] = context.reported_buy_proposal_records["holdings"]
    assert row["proposal_ref"] == _ref(f.b).model_dump(mode="json")
    assert len(row["records"]["corrections"]) == 1


def test_receipt_cutoff_keeps_later_correction_out_of_earlier_authoring(tmp_path):
    f = _setup(tmp_path)
    _correct(f)
    before = _context(f, now=RECEIPT - timedelta(microseconds=1)).payload()
    at = _context(f, now=RECEIPT).payload()
    assert before["reported_buy_proposal_records"]["holdings"][0]["records"]["corrections"] == []
    assert len(at["reported_buy_proposal_records"]["holdings"][0]["records"]["corrections"]) == 1


def test_nonpurchase_report_and_replacement_membership_do_not_erase_buy_report(tmp_path):
    f = _setup(tmp_path, linked="replacement")
    correction = _correct(f)
    report = ProposalRecordService(f.case.app, clock=lambda: RECEIPT).report(
        HumanProposalReportDraft(
            report_id="synthetic-nonpurchase",
            proposal_ref=_ref(f.a),
            outcome="not_purchased",
            statement="合成：その提案では買わなかったという別報告。台帳との整合は別に確認する。",
            reported_at=START,
        )
    )
    context = _context(f)
    [row] = context.payload()["reported_buy_proposal_records"]["holdings"]
    assert row["proposal_ref"] == _ref(f.a).model_dump(mode="json")
    assert row["records"]["corrections"] == [correction.model_dump(mode="json")]
    assert row["records"]["human_reports"] == [report.model_dump(mode="json")]
    assert context.inputs.positions[0].quantity == 100
    assert context.inputs.positions[0].original_assessment_ref == f.a.items[0].assessment_ref


@pytest.mark.parametrize(
    ("scope", "linked"), [("specified_stocks", "erroneous"), ("review_positions", "unlinked")]
)
def test_only_linked_target_holdings_get_reported_proposal_records(tmp_path, scope, linked):
    f = _setup(tmp_path, scope=scope, linked=linked)
    _correct(f)
    context = _context(f)
    assert context.payload()["reported_buy_proposal_records"]["holdings"] == []
    assert context.inputs.positions[0].quantity == 100


@pytest.mark.parametrize("error_type", [ValueError, FileNotFoundError])
def test_unreadable_optional_records_are_not_no_correction_or_new_action_gate(
    tmp_path, monkeypatch, error_type
):
    f = _setup(tmp_path)
    before = _context(f)

    def unreadable(*_args, **_kwargs):
        raise error_type("synthetic record conflict")

    monkeypatch.setattr(input_reader, "proposal_records", unreadable)
    context = _context(f)
    [row] = context.payload()["reported_buy_proposal_records"]["holdings"]
    assert row["status"] == "unavailable"
    assert row["records"] is None
    assert "synthetic record conflict" in row["unavailable_reason"]
    assert context.inputs == before.inputs
    assert context.calculations == before.calculations
