"""調整後の分析用観測を残し、価値計算に選ぶ未調整価格とは分ける。全て合成。"""

from contextlib import closing
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from tests.helpers.assessment_v1 import NOW, assessment_id, pair_payload, update_review_hash
from tests.helpers.generated_selection import generated_draft_for, seed_generated_selection

from baibai_engine.investment_judgment.company_research.document import ResearchDraft
from baibai_engine.investment_judgment.company_research.store import ResearchStoreService
from baibai_engine.investment_judgment.independent_review.contracts import AssessmentReview
from baibai_engine.investment_judgment.stock_assessment.calculation import (
    CalculationInput,
    calculate,
)
from baibai_engine.investment_judgment.stock_assessment.document import (
    StockAssessmentDocument,
    UnpublishedAssessment,
    current_price_projection,
    evaluate_assessment,
)
from baibai_engine.investment_judgment.stock_assessment.store import (
    AssessmentStoreService,
    load_reviewed_assessment,
)
from baibai_engine.investment_judgment.workflow.workspace import (
    prepare_selected_workspace,
    scaffold_assessment,
)
from baibai_engine.storage.sqlite.application.read import connect_read_only


def _pair(*, unresolved=False):
    raw, review = pair_payload(price_case="undetermined" if unresolved else "supported_at_quote")
    if unresolved:
        raw["valuation"] = {
            "status": "unresolved",
            "market_price_fact_id": None,
            "horizon_months": None,
            "required_annual_return_pct": None,
            "base": None,
            "downside": None,
            "unresolved_reason": "調整後価格と未採択cash条件しかなく、価値・未調整価格は未確定。",
        }
        raw["conclusion"]["stance"] = "uncertain"
        raw["investment_case"]["status"] = "uncertain"
        review["primary_source_check"] = "partially_verified"
        review["recalculated_projections"] = []
    return raw, review


def _evaluation(raw, review):
    update_review_hash(raw, review)
    return evaluate_assessment(
        StockAssessmentDocument.model_validate(raw),
        review=AssessmentReview.model_validate(review),
        identity=UnpublishedAssessment.DRAFT,
        now=NOW,
    )


def _arithmetic(raw):
    return calculate(
        CalculationInput.model_validate(
            {"input_snapshot": raw["input_snapshot"], "valuation": raw["valuation"]}
        )
    )


def test_unresolved_assessment_keeps_adjusted_observation_without_price_adoption():
    raw, review = _pair(unresolved=True)
    raw["input_snapshot"]["facts"][0]["price_basis"] = "last_close_adjusted"
    assert _evaluation(raw, review).errors == ()
    document = StockAssessmentDocument.model_validate(raw)
    assert document.input_snapshot.facts[0].price_basis == "last_close_adjusted"
    assert document.valuation.market_price_fact_id is None
    assert _arithmetic(raw)["projection_numbers"] is None


def test_context_adjusted_price_does_not_change_separately_selected_unadjusted_arithmetic():
    raw, review = _pair()
    before = _arithmetic(raw)
    analytical = {**raw["input_snapshot"]["facts"][0], "fact_id": "analytical_close"}
    analytical.update(price_basis="last_close_adjusted", value=550)
    raw["input_snapshot"]["facts"].append(analytical)
    assert _evaluation(raw, review).errors == ()
    assert _arithmetic(raw) == before
    assert raw["valuation"]["market_price_fact_id"] == "close"


@pytest.mark.parametrize("unresolved", [False, True])
def test_selected_adjusted_price_still_cannot_be_a_valuation_price(unresolved):
    raw, review = _pair(unresolved=unresolved)
    raw["input_snapshot"]["facts"][0]["price_basis"] = "last_close_adjusted"
    raw["valuation"]["market_price_fact_id"] = "close"
    assert "market_price requires unadjusted JPY_per_share basis" in _evaluation(raw, review).errors
    if not unresolved:
        with pytest.raises(ValueError, match="numeric unadjusted market_price"):
            _arithmetic(raw)
    assert (
        current_price_projection(
            StockAssessmentDocument.model_validate(raw),
            price_yen=Decimal(1100),
            price_as_of=NOW.date(),
            as_of=NOW.date(),
            basis_confirmed=True,
            max_quote_age_days=3,
            price_basis="last_close_adjusted",
        )
        is None
    )


@pytest.mark.parametrize(
    ("field", "value"), [("unit", "JPY"), ("value", 0), ("value", "unknown"), ("price_basis", None)]
)
def test_contextual_price_still_requires_numeric_positive_price_and_explicit_basis(field, value):
    raw, review = _pair(unresolved=True)
    fact = raw["input_snapshot"]["facts"][0]
    fact["price_basis"] = "last_close_adjusted"
    fact[field] = value
    if field == "price_basis":
        with pytest.raises(ValueError, match="market_price requires price_basis"):
            _evaluation(raw, review)
    else:
        assert _evaluation(raw, review).errors


def test_adjusted_close_not_available_before_session_close_is_still_rejected():
    raw, review = _pair(unresolved=True)
    raw["input_snapshot"]["facts"][0].update(price_basis="last_close_adjusted", observed_at=None)
    raw["conclusion"]["assessed_at"] = "2026-10-14T10:00:00+09:00"
    for source in raw["input_snapshot"]["sources"]:
        source["retrieved_at"] = "2026-10-14T09:00:00+09:00"
    assert (
        "market_price close date follows the available session" in _evaluation(raw, review).errors
    )


def test_normal_research_scaffold_and_reviewed_save_keep_context_basis_and_exact_retry(tmp_path):
    raw, review = _pair(unresolved=True)
    fact = deepcopy(raw["input_snapshot"]["facts"][0])
    fact["price_basis"] = "last_close_adjusted"
    db, runs, workspace = tmp_path / "app", tmp_path / "runs", tmp_path / "workspace"
    research = ResearchStoreService(db, clock=lambda: NOW - timedelta(minutes=1)).save(
        ResearchDraft.model_validate(
            {
                "research_id": "synthetic-adjusted-price-context",
                "document": {
                    "ticker": "1234",
                    "as_of": NOW.date().isoformat(),
                    "investigated_scope": "合成分析用価格のbasis保持のみ。価格・価値採択なし。",
                    "sources": [raw["input_snapshot"]["sources"][1]],
                    "facts": [fact],
                    "unresolved_questions": ["同一起点の未調整価格と継続cashを確認できるか"],
                },
            }
        )
    )
    selected = seed_generated_selection(runs=runs, now=NOW, tickers=("1234",), db=db)
    prepare_selected_workspace(
        draft=generated_draft_for(selected),
        db_path=db,
        workspace=workspace,
        runs_path=runs,
        now=NOW,
    )
    result = scaffold_assessment(
        workspace=workspace,
        ticker="1234",
        sqlite_path=tmp_path / "absent-market",
        as_of=NOW.date(),
        retrieved_at=NOW,
        db_path=db,
        from_research_id=research.reference.research_id,
    )
    scaffold = yaml.safe_load(Path(result["assessment_draft"]).read_text())
    assert scaffold["valuation"]["market_price_fact_id"] is None
    copied = scaffold["input_snapshot"]["facts"][0]
    assert copied == {**fact, "fact_id": "research_close", "source_ids": ["research_price"]}
    raw["input_snapshot"]["sources"].extend(scaffold["input_snapshot"]["sources"])
    raw["input_snapshot"]["facts"] = [copied]
    review["checked_source_ids"].append("research_price")
    update_review_hash(raw, review)
    writer = AssessmentStoreService(db, clock=lambda: NOW)
    pair = writer.publish_reviewed_assessment(assessment_id(), raw, review)
    assert pair.document.valuation.status == "unresolved"
    assert pair.document.input_snapshot.facts[0].price_basis == "last_close_adjusted"
    assert writer.publish_reviewed_assessment(assessment_id(), raw, review) == pair
    with closing(connect_read_only(db)) as connection:
        assert load_reviewed_assessment(connection, assessment_id()) == pair
