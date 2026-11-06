"""A received action can change units before the next eligible raw close exists."""

import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import backend_case, bind_review, reviewed_proposal
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.investment_judgment.stock_assessment.document import StockAssessmentDocument
from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.conditions import assessment_quote_basis
from baibai_engine.investment_judgment.trade_proposal.contracts import QuoteInput
from baibai_engine.investment_judgment.trade_proposal.entry_policy import evaluate_buy_terms
from baibai_engine.investment_judgment.trade_proposal.inputs import build_proposal_context
from baibai_engine.investment_judgment.trade_proposal.preview import preview_current_terms
from baibai_engine.investment_judgment.trade_proposal.query import get_investment_proposal
from baibai_engine.investment_judgment.trade_proposal.scaffold import scaffold_proposal
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_engine.portfolio.holdings.market_observations import read_unadjusted_close
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only


@pytest.mark.parametrize(
    ("action_day", "factor", "now_text", "expected"),
    [
        (None, None, "2026-10-15T10:00:00+09:00", True),
        ("2026-10-15", 1.0, "2026-10-15T10:00:00+09:00", True),
        ("2026-10-15", 0.5, "2026-10-15T10:00:00+09:00", False),
        ("2026-10-15", 2.0, "2026-10-15T10:00:00+09:00", False),
        ("2026-10-15", None, "2026-10-15T10:00:00+09:00", False),
        ("2026-10-16", 0.5, "2026-10-15T10:00:00+09:00", True),
        ("2026-10-15", 0.5, "2026-10-14T15:30:00+00:00", False),
        ("2026-10-14", 0.5, "2026-10-15T10:00:00+09:00", True),
    ],
)
def test_buy_basis_checks_received_actions_without_requiring_today_close(
    tmp_path, action_day, factor, now_text, expected
):
    path = tmp_path / "market.sqlite"
    rows = [("1234", "2026-10-14", 1100.0, factor if action_day == "2026-10-14" else 1.0)]
    if action_day is not None and action_day != "2026-10-14":
        rows.append(("1234", action_day, None, factor))
    seed_daily_bars(path, rows)
    now = datetime.fromisoformat(now_text)
    raw, _ = pair_payload(as_of="2026-10-15", quote_as_of="2026-10-14")
    assessment = StockAssessmentDocument.model_validate(raw)
    with closing(connect_read_only(path)) as connection:
        connection.execute("BEGIN")
        observed = read_unadjusted_close(
            sqlite_path=path, ticker="1234", at=now, connection=connection
        )
        assert observed is not None
        assert observed.price_as_of == date(2026, 10, 14)
        quote = QuoteInput(
            ticker="1234",
            price_yen=Decimal(str(observed.close_yen)),
            price_as_of=observed.price_as_of,
            price_basis="last_close_unadjusted",
            source_ref="synthetic",
        )
        basis = assessment_quote_basis(
            assessment, quote, sqlite_path=path, now=now, market_connection=connection
        )
    assert basis is expected
    terms = evaluate_buy_terms(
        assessment,
        reviewed=True,
        latest=True,
        as_of=date(2026, 10, 15),
        quote=quote,
        proposed_limit_yen=Decimal(1000),
        basis_confirmed=basis,
        market_price_max_age_days=5,
        held_quantity=0,
        active_reservation=False,
        proposal_executed=False,
        available_cash_yen=Decimal(300000),
        budget_max_yen=Decimal(300000),
        board_lot=100,
    )
    assert [reason.code for reason in terms.hard_blockers] == (
        [] if expected else ["corporate_action_unresolved"]
    )
    assert terms.quantity == (300 if expected else 0)


def _morning(clock):
    return datetime.fromisoformat(f"2026-10-15T{clock}+09:00")


@pytest.mark.parametrize("mode", ["buy", "holding_return", "holding_case", "no_action"])
def test_fresh_morning_proposal_observes_received_price_action(tmp_path, mode):
    holding = mode.startswith("holding")
    action = "hold" if holding else mode
    case = backend_case(tmp_path, holding=holding)
    template = reviewed_proposal(case, action=action).payload()
    document, review = pair_payload(as_of="2026-10-15", quote_as_of="2026-10-14", review_number=3)
    for source in document["input_snapshot"]["sources"]:
        source["retrieved_at"] = _morning("09:00:00").isoformat()
    document["conclusion"]["assessed_at"] = _morning("09:00:00").isoformat()
    review["reviewed_at"] = _morning("09:05:00").isoformat()
    update_review_hash(document, review)
    pair = AssessmentStoreService(
        case.app, clock=lambda: _morning("09:06:00")
    ).publish_reviewed_assessment(
        assessment_id(3), document, review, supersedes_id=case.pair.assessment_id
    )
    case.operations.record_assessment(
        case.operation.operation_id, pair.assessment_id, now=_morning("09:10:00")
    )
    raw = scaffold_proposal(
        operation_id=case.operation.operation_id,
        proposal_id=template["proposal_id"],
        db_path=case.app,
        sqlite_path=case.market,
        now=_morning("09:20:00"),
    )
    for key in ("headline", "comparison", "forgone"):
        raw[key] = template[key]
    item = template["items"][0]
    item["assessment_ref"] = raw["items"][0]["assessment_ref"]
    if holding:
        item["position"] = raw["inputs"]["positions"][0]
    if mode == "holding_return":
        item["holding_basis"] = "sufficient_remaining_reward"
        item["remaining_reward"] = {"status": "sufficient", "reason": "合成当日算術を確認。"}
    if mode == "buy":
        item["terms"]["valuation_as_of"] = "2026-10-15"
    raw["items"] = [item]
    raw["review"]["reviewed_at"] = _morning("09:25:00").isoformat()
    raw["review"]["reviewer_identity"] = "synthetic-independent-morning-reviewer"
    draft = bind_review(raw, {pair.assessment_id: pair.document})
    service = TradeProposalService(
        case.app, sqlite_path=case.market, clock=lambda: _morning("09:30:00")
    )
    assert service.check(draft).valid
    saved = service.publish(draft)
    seed_daily_bars(case.market, [("1234", "2026-10-15", None, 0.8)])
    with closing(sqlite3.connect(case.market)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET ex_right_type='3' WHERE traded_at='2026-10-15'"
        )
    # Rights price adjustments leave held quantities unchanged. Only the price
    # comparison fails, without a quantity-basis or date-refresh blocker.
    before = case.app.read_bytes()
    check = service.check(draft)
    codes = {reason.code for reason in check.blockers}
    assert "valuation_basis_requires_refresh" not in codes
    assert "quantity_basis_unconfirmed" not in codes
    assert ("corporate_action_unresolved" in codes) == (mode in ("buy", "holding_return"))
    assert check.valid == (mode in ("holding_case", "no_action"))
    context = build_proposal_context(
        operation_id=case.operation.operation_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=_morning("09:35:00"),
    )
    assert context.calculations["1234"]["current_quote_projection"] is None
    preview = preview_current_terms(
        proposal_id=saved.proposal_id,
        db_path=case.app,
        sqlite_path=case.market,
        now=_morning("09:35:00"),
    )
    assert preview.current_inputs.quotes[0].price_as_of == date(2026, 10, 14)
    row = preview.items[0]
    if mode == "buy":
        assert row["saved_limit_revalidation"]["quantity"] == 0
    elif holding:
        assert row["current_quote_calculation"]["current_price_projection"] is None
    else:
        assert row["current_quote_calculation"] is None
    assert get_investment_proposal(proposal_id=saved.proposal_id, db_path=case.app) == saved
    assert case.app.read_bytes() == before
