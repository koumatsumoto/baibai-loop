"""公開実銘柄shadowの構造と購入境界を検証。経済仮定の正しさや本番実行は証明しない。"""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from baibai_engine.investment_judgment.trade_proposal.entry_policy import evaluate_buy_terms
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    UnpublishedAssessment,
    assessment_core_hash,
    evaluate_assessment,
)
from baibai_engine.investment_judgment.trade_proposal.contracts import QuoteInput

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/research/strategy-shadow"


@pytest.mark.parametrize("ticker", ["4783", "4559"])
def test_public_shadow_retains_unreviewed_status_and_economic_blockers(ticker):
    payload = json.loads((FIXTURES / f"shadow-{ticker}.json").read_text())
    document = StockAssessmentDocument.model_validate(payload["document"])
    original_hash = assessment_core_hash(document)
    assert payload["review"] is None
    assert StockAssessmentDocument.model_validate_json(document.model_dump_json()) == document
    result = evaluate_assessment(
        document,
        identity=UnpublishedAssessment.DRAFT,
        now=datetime.fromisoformat("2026-10-20T13:00:00+00:00"),
    )
    assert result.errors == ("Reviewed Assessment requires an Assessment Review",)
    assert document.conclusion.stance == "uncertain"
    assert document.investment_case.status == "uncertain"
    price = next(f for f in document.input_snapshot.facts if f.fact_kind == "market_price")
    quote = QuoteInput(
        ticker=ticker,
        price_yen=price.value,
        price_as_of=price.as_of,
        price_basis="last_close_unadjusted",
        source_ref="public-shadow-fixed-close",
    )
    # 資金・単元は合成入力。安いlimitや潤沢なcashでも未解決の企業評価を購入可にしない。
    for limit in (Decimal(100), Decimal(str(price.value))):
        terms = evaluate_buy_terms(
            document,
            reviewed=False,
            latest=True,
            as_of=date(2026, 10, 16),
            quote=quote,
            proposed_limit_yen=limit,
            basis_confirmed=True,
            market_price_max_age_days=7,
            held_quantity=0,
            active_reservation=False,
            proposal_executed=False,
            available_cash_yen=Decimal(10000000),
            budget_max_yen=Decimal(1000000),
            board_lot=100,
        )
        codes = {blocker.code for blocker in terms.hard_blockers}
        assert "reviewed_assessment_required" in codes
        assert set(payload["expected_blockers"]).issubset(codes)
        assert terms.quantity == 0
        if ticker == "4783":
            assert terms.maximum_price_yen is not None
            assert Decimal(2518) < terms.maximum_price_yen < Decimal(2519)
        else:
            assert terms.maximum_price_yen is None
            assert document.valuation.base is None
    assert assessment_core_hash(document) == original_hash
