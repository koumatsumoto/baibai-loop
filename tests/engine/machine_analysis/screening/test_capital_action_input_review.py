"""資本異動後の自己株未確認は事業否定ではなく、既存の入力確認へ残す。"""

from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from tests.engine.machine_analysis.screening.test_financial_input_review import (
    case,
    current,
    project_unchanged,
)
from tests.engine.machine_analysis.screening.test_full_fy_fallback import AS_OF, documents
from tests.engine.machine_analysis.screening.test_unresolved_financial_input_review import (
    unresolved_case,
)
from tests.helpers.archived_technical_financial import stored_result
from tests.helpers.model_generation import materialize_generation

from baibai_engine.machine_analysis.generation.assembly import CompanyModelInputs, generate_models
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials.capital_action_pdf import CapitalAction
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.screening.policies.technical_financial_selection import (
    project_technical_financial_row,
)

REASON = "indeterminate_treasury_after_capital_action"


@pytest.mark.parametrize("unresolved", [False, True])
@pytest.mark.parametrize("expansion", [False, True])
@pytest.mark.parametrize("technical", [False, True])
def test_capital_action_gap_keeps_existing_hypothesis_without_clearing_failures(
    unresolved, expansion, technical
):
    data = (
        unresolved_case(expansion=expansion, technical=technical)
        if unresolved
        else case(expansion=expansion, technical=technical, reason="missing_capital_basis")
    )
    expected = project_unchanged(data)
    data["company_financials"]["earnings_price"]["unavailable_reason"] = REASON
    actual = project_unchanged(data)
    assert actual.financial_gate == expected.financial_gate
    assert actual.technical == expected.technical
    assert actual.financial_paths["paths"] == expected.financial_paths["paths"]
    assert actual.financial_paths["matched_routes"] == []
    assert actual.screening_lane == "input_review"
    reasons = deepcopy(expected.financial_paths["input_review_reasons"])
    for reason in reasons:
        reason["reason"] = REASON
    assert actual.financial_paths["input_review_reasons"] == reasons
    assert data["company_financials"]["earnings_price"]["shares_ex_treasury"] is None
    assert data["company_financials"]["earnings_price"]["market_cap_yen"] is None


@pytest.mark.parametrize(
    "problem",
    ["conflicting_actions", "invalid_shares", "unknown_reason", "missing_business", "old_price"],
)
def test_action_word_or_missing_coordinate_is_not_blanket_input_review(problem):
    data = case(reason=REASON, technical=False)
    earnings = data["company_financials"]["earnings_price"]
    if problem == "conflicting_actions":
        earnings["unavailable_reason"] = "conflicting_capital_actions"
    elif problem == "invalid_shares":
        earnings["shares_ex_treasury"] = 0.0
    elif problem == "unknown_reason":
        earnings["unavailable_reason"] = "unknown_capital_action_failure"
    elif problem == "missing_business":
        current(data, "annual_ttm")["metrics"]["operating_margin"].update(
            value=None, reason="not_observed"
        )
    else:
        earnings["price_date"] = "2026-11-12"
    row = project_unchanged(data)
    assert row.screening_lane == "no_current_hypothesis"
    assert "input_review_reasons" not in row.financial_paths


@pytest.mark.parametrize("expansion", [False, True])
def test_complete_share_basis_does_not_stay_in_input_review(expansion):
    missing = case(reason=REASON, expansion=expansion, technical=False)
    valid = case(expansion=expansion, technical=False)
    before = project_unchanged(missing)
    after = project_unchanged(valid)
    assert before.financial_gate == after.financial_gate
    assert before.technical == after.technical
    assert before.screening_lane == "input_review"
    assert after.screening_lane == "alternative_financial_research"
    assert after.financial_paths["matched_routes"] == [
        "business_expansion" if expansion else "earning_power"
    ]
    assert "input_review_reasons" not in after.financial_paths


def native_generation(*, completed_action):
    action = CapitalAction(
        ticker="1001",
        issued_shares=130,
        effective_date=date(2025, 7, 1),
        published_on=date(2025, 7, 2),
        received_at=datetime(2025, 7, 2, tzinfo=UTC),
        source_url="https://example.invalid/synthetic",
        source_sha256="a" * 64,
        disclosure_number="synthetic-completed-conversion",
        description="completed common-share conversion",
    )
    release = InputRelease(release_id="capital-review-fixture", manifest_sha256="b" * 64)
    return materialize_generation(
        generate_models(
            generation_id="capital-review-fixture-generation",
            as_of=AS_OF,
            evaluation_time=datetime(2025, 9, 1, tzinfo=UTC),
            inputs={
                "market": InputSnapshot(
                    kind="lake_release",
                    reference=release.release_id,
                    sha256=release.manifest_sha256,
                )
            },
            financial_release=release,
            companies=(
                CompanyModelInputs(
                    ticker="1001",
                    financial_documents=documents(),
                    adjusted_bars=(PriceBar(AS_OF, 10.0, None),),
                    adjustment_events=(),
                    sector="情報・通信業",
                    capital_actions=(action,) if completed_action else (),
                ),
            ),
            trading_dates=(AS_OF,),
            expected_price_date=AS_OF,
            ttm_rules=DEFAULT_TTM_RULES,
            external_observations={},
            market_context=None,
            securities_context={"1001": {"master": {"sector_33": "情報・通信業"}}},
            market_context_revision="synthetic-v1",
        )
    )


def test_normal_generation_retains_action_unknown_in_review_not_pricing():
    control = native_generation(completed_action=False)
    fixed = native_generation(completed_action=True)
    before = fixed.model_dump_json()
    row = project_technical_financial_row("1001", models=fixed.models, metadata=fixed)
    original = project_technical_financial_row("1001", models=control.models, metadata=control)
    assert row.financial_gate == original.financial_gate
    assert row.financial_gate["state"] == "input_unresolved"
    assert row.technical["state"] == "unavailable"
    assert row.screening_lane == "input_review"
    assert row.financial_paths["matched_routes"] == []
    assert {r["path_id"] for r in row.financial_paths["input_review_reasons"]} == {
        "earning_power",
        "business_expansion",
    }
    assert {r["reason"] for r in row.financial_paths["input_review_reasons"]} == {REASON}
    selected = stored_result((row,), order=("1001",))
    assert selected.lane_tickers("input_review") == ("1001",)
    cf = next(model.payload for model in fixed.models if model.model_id == "company_financials")
    assert cf["earnings_price"]["unavailable_reason"] == REASON
    for key in ("shares_ex_treasury", "market_cap_yen", "earnings_yield"):
        assert cf["earnings_price"][key] is None
    assert cf["capital_action_evidence"][0]["issued_shares"] == 130
    assert fixed.model_dump_json() == before
