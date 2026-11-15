"""指値の時間感応度は旧評価価格の年率や新しい期間採択で代用しない。"""

from datetime import timedelta
from decimal import Decimal, localcontext

import pytest
from tests.helpers.assessment_v1 import assessment_id, pair_payload, update_review_hash
from tests.helpers.proposal import PREPARED, PUBLISHED, backend_case, bind_review, reviewed_proposal

from baibai_engine.investment_judgment.stock_assessment.store import AssessmentStoreService
from baibai_engine.investment_judgment.trade_proposal.service import TradeProposalService
from baibai_web.readmodel.proposal_models import SavedBuyCapitalComparisonView
from baibai_web.sources.proposal import DbProposalSource


@pytest.mark.parametrize("limit", ["900", "1000"])
def test_normal_saved_buy_keeps_limit_specific_time_sensitivity(tmp_path, limit):
    case = backend_case(tmp_path)
    raw = reviewed_proposal(case).payload()
    raw["items"][0]["terms"].update(
        limit_price_yen=limit, quantity=100, notional_yen=str(Decimal(limit) * 100)
    )
    draft = bind_review(raw, {case.pair.assessment_id: case.pair.document})
    service = TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED)
    checked = service.check(draft)
    assert checked.valid
    saved = service.publish(draft)

    later_document, later_review = pair_payload(review_number=21)
    later_document["valuation"]["horizon_months"] = 36
    update_review_hash(later_document, later_review)
    AssessmentStoreService(
        case.app, clock=lambda: PUBLISHED + timedelta(minutes=1)
    ).publish_reviewed_assessment(
        assessment_id(20), later_document, later_review, supersedes_id=case.pair.assessment_id
    )
    before = case.app.read_bytes()
    view = DbProposalSource(case.app).latest()
    item = view.items[0]
    comparison = item.saved_buy_comparison
    sensitivity = comparison.model_dump(mode="json").get("time_sensitivity")
    assert sensitivity is not None
    assert sensitivity["assumption"] == "terminal_and_cumulative_cash_unchanged"
    assert sensitivity["additional_months"] == 12
    assert sensitivity["horizon_months"] == 24
    assert comparison.horizon_months == item.assessment.valuation.horizon_months == 12
    assert item.assessment.valuation.price_yen == "1100"
    assert item.assessment.core_sha256 == case.pair.core_sha256
    for name, wealth in (("base", 1100), ("downside", 700)):
        with localcontext() as context:
            context.prec = 50
            ratio = Decimal(wealth) / Decimal(limit)
            annual = (ratio - 1) * 100
            delayed = (ratio.sqrt() - 1) * 100
        row = sensitivity[name]
        assert Decimal(row["annualized_return_pct"]) == round(annual, 4)
        assert Decimal(row["delayed_annualized_return_pct"]) == round(delayed, 4)
        existing = getattr(comparison, name)
        assert Decimal(existing.profit_loss_yen) == (Decimal(wealth) - Decimal(limit)) * 100
        if name == "downside":
            assert annual < delayed < 0
            assert Decimal(existing.profit_loss_yen) < 0
    original_calculation = checked.calculations[item.ticker]["limit_price_projection"]
    assert Decimal(sensitivity["base"]["delayed_annualized_return_pct"]) == Decimal(
        str(original_calculation["fixed_value_delay"]["base_annualized_return_pct"])
    )
    assert item.action == "buy"
    assert item.terms.quantity == 100
    assert Decimal(item.terms.limit_price_yen) == Decimal(limit)
    assert service.publish(draft) == saved
    assert case.app.read_bytes() == before
    case.market.rename(tmp_path / "unused-market.sqlite")
    assert DbProposalSource(case.app).latest() == view
    assert case.app.read_bytes() == before


def test_zero_downside_stays_total_loss_at_both_horizons(tmp_path):
    case = backend_case(tmp_path)
    document, review = pair_payload(review_number=11)
    document["valuation"]["downside"].update(
        terminal_value_per_share_yen=0, cash_distribution_per_share_yen=0
    )
    review["recalculated_projections"][1].update(
        terminal_value_per_share_yen=0, cash_distribution_per_share_yen=0
    )
    update_review_hash(document, review)
    case.pair = AssessmentStoreService(
        case.app, clock=lambda: PREPARED
    ).publish_reviewed_assessment(
        assessment_id(10), document, review, supersedes_id=case.pair.assessment_id
    )
    case.operations.record_assessment(
        case.operation.operation_id, case.pair.assessment_id, now=PREPARED
    )
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        reviewed_proposal(case)
    )
    comparison = DbProposalSource(case.app).latest().items[0].saved_buy_comparison
    sensitivity = comparison.model_dump(mode="json").get("time_sensitivity")
    assert sensitivity is not None
    assert Decimal(sensitivity["downside"]["annualized_return_pct"]) == -100
    assert Decimal(sensitivity["downside"]["delayed_annualized_return_pct"]) == -100
    assert Decimal(comparison.downside.profit_loss_yen) == -Decimal(comparison.notional_yen)


@pytest.mark.parametrize("mode", ["absent", "null"])
def test_old_comparison_does_not_infer_time_sensitivity(tmp_path, mode):
    case = backend_case(tmp_path)
    TradeProposalService(case.app, sqlite_path=case.market, clock=lambda: PUBLISHED).publish(
        reviewed_proposal(case)
    )
    raw = DbProposalSource(case.app).latest().items[0].saved_buy_comparison.model_dump(mode="json")
    raw["time_sensitivity"] = None
    if mode == "absent":
        del raw["time_sensitivity"]
    parsed = SavedBuyCapitalComparisonView.model_validate(raw)
    assert parsed.time_sensitivity is None
    assert parsed.base.profit_loss_yen == raw["base"]["profit_loss_yen"]
