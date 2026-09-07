"""Current entry and holding rules: F04/F05/F09/F11/F12/F13/F17."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from tests.helpers.research_v4 import pair_payload

from baibai_engine.research.entry_policy import evaluate_entry
from baibai_engine.research.position_review import (
    HoldingInput,
    PositionReviewDocument,
    QuoteInput,
    RemainingReward,
    evaluate_position_review,
)
from baibai_engine.research.thesis import ThesisDocument


def entry(**changes):
    thesis = ThesisDocument.model_validate(pair_payload()[0])
    args = dict(
        reviewed=True,
        latest=True,
        as_of=date(2026, 9, 7),
        price_yen=Decimal(1000),
        price_as_of=date(2026, 9, 4),
        basis_confirmed=True,
        minimum_required_annual_return_pct=Decimal("8.5"),
        market_price_max_age_days=7,
        held_quantity=0,
        active_reservation=False,
        assessment_executed=False,
        available_cash_yen=Decimal(500000),
        budget_max_yen=Decimal(300000),
        board_lot=100,
    )
    args.update(changes)
    return thesis, evaluate_entry(thesis, **args)


@pytest.mark.parametrize(
    ("changes", "quantity", "reason"),
    [
        ({}, 300, None),
        ({"available_cash_yen": Decimal(200000)}, 200, None),
        ({"available_cash_yen": Decimal(99999)}, 0, "available_cash_below_board_lot"),
        ({"price_yen": Decimal("1000.5")}, 200, None),
        ({"price_yen": Decimal(1102)}, 0, "price_above_pmax"),
        ({"held_quantity": 100}, 0, "already_held"),
        ({"active_reservation": True}, 0, "active_reservation_exists"),
        ({"assessment_executed": True}, 0, "assessment_already_executed"),
        ({"latest": False}, 0, "latest_thesis_required"),
        ({"as_of": date(2026, 9, 8)}, 0, "valuation_basis_requires_refresh"),
        (
            {"minimum_required_annual_return_pct": Decimal(13)},
            0,
            "required_return_below_current_policy",
        ),
    ],
)
def test_current_entry_without_mutating_enterprise(changes, quantity, reason):
    thesis, result = entry(**changes)
    assert result.quantity == quantity
    assert thesis.judgment.disposition == "candidate"
    assert (reason in result.reasons) if reason else result.eligible
    assert result.maximum_price_yen == entry()[1].maximum_price_yen


@pytest.mark.parametrize(
    ("status", "remaining", "resolved", "expected"),
    [
        ("broken", None, False, "exit"),
        ("intact", "sufficient", True, "hold"),
        ("intact", "insufficient", True, "exit"),
        ("intact", "uncertain", True, None),
        ("uncertain", "sufficient", True, None),
        ("intact", "sufficient", False, None),
    ],
)
def test_holding_economic_rule(status, remaining, resolved, expected):
    payload, _ = pair_payload()
    payload["investment_case"]["status"] = status
    payload["judgment"]["disposition"] = "reject" if status == "broken" else "defer"
    if not resolved:
        payload["valuation"] = dict(
            status="unresolved",
            market_price_fact_id=None,
            horizon_months=None,
            required_annual_return_pct=None,
            base=None,
            downside=None,
            unresolved_reason="評価不明",
        )
    thesis = ThesisDocument.model_validate(payload)
    document = PositionReviewDocument(
        schema_version=3,
        position_review_id="review",
        thesis_id="thesis",
        position_id="position",
        ticker="1234",
        as_of=date(2026, 9, 7),
        holding=HoldingInput(
            quantity=100, cost_yen=Decimal(100000), quantity_basis_confirmed=resolved
        ),
        quote=QuoteInput(
            price_yen=Decimal(1250),
            observed_at=datetime.fromisoformat("2026-09-04T15:30:00+09:00"),
            price_basis="last_close_unadjusted",
            source_ref="quote",
            basis_confirmed=True,
        )
        if resolved
        else None,
        remaining_reward=None
        if remaining is None
        else RemainingReward(status=remaining, reason="将来分配のみ・税費用・遅延を確認"),
        action=expected,
    )
    result = evaluate_position_review(document, thesis, primary_verified=True)
    assert result.action == expected
    if status == "broken" and not resolved:
        assert result.sell_quantity is None
