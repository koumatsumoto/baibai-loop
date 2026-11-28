"""資本異動後の自己株未確認は事業否定ではなく、既存の入力確認へ残す。"""

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
from tests.helpers.model_generation import materialize_generation

from baibai_engine.machine_analysis.generation.assembly import CompanyModelInputs, generate_models
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials.capital_action_pdf import CapitalAction
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.screening.policies.discovery import project_discovery_row

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
    assert "financial:missing_capital_basis" in expected["missing"]
    assert actual == {
        **expected,
        "missing": sorted(
            f"financial:{REASON}" if v == "financial:missing_capital_basis" else v
            for v in expected["missing"]
        ),
    }
    assert actual["matched_paths"]["financial"] == actual["coordinates"] == []
    assert actual["screening_lane"] == "input_review"
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
    assert row["screening_lane"] == "no_current_hypothesis"
    assert not any(k.startswith("financial:") for k in row["missing"])


@pytest.mark.parametrize("expansion", [False, True])
def test_complete_share_basis_does_not_stay_in_input_review(expansion):
    missing = case(reason=REASON, expansion=expansion, technical=False)
    valid = case(expansion=expansion, technical=False)
    before = project_unchanged(missing)
    after = project_unchanged(valid)
    assert before["financial_state"] == after["financial_state"]
    assert before["technical_state"] == after["technical_state"]
    assert before["screening_lane"] == "input_review"
    assert after["screening_lane"] == "alternative_financial_research"
    assert after["matched_paths"]["financial"] == [
        "business_expansion" if expansion else "earning_power"
    ]
    assert not any(k.startswith("financial:") for k in after["missing"])


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
            subjects=("1001",),
            companies=(
                CompanyModelInputs(
                    ticker="1001",
                    financial_documents=documents(),
                    adjusted_bars=(PriceBar(AS_OF, 10.0, None),),
                    adjustment_events=(),
                    sector="情報・通信業",
                    capital_actions=(action,) if completed_action else (),
                    market_context={"master": {"sector_33": "情報・通信業"}},
                ),
            ),
            trading_dates=(AS_OF,),
            expected_price_date=AS_OF,
            ttm_rules=DEFAULT_TTM_RULES,
            external_observations={},
            market_context=None,
            market_context_revision="synthetic-v1",
        )
    )


def test_normal_generation_retains_action_unknown_in_review_not_pricing():
    control = native_generation(completed_action=False)
    fixed = native_generation(completed_action=True)
    before = fixed.model_dump_json()
    row = project_discovery_row("1001", models=fixed.models, metadata=fixed)
    original = project_discovery_row("1001", models=control.models, metadata=control)
    assert row["financial_state"] == original["financial_state"] == "input_unresolved"
    assert row["technical_state"] == "unavailable"
    assert row["screening_lane"] == "input_review"
    assert row["matched_paths"]["financial"] == row["coordinates"] == []
    assert [s for s in row["missing"] if s.startswith("financial:")] == [f"financial:{REASON}"]
    assert row["capital_action"]["observations"] == 1
    cf = next(model.payload for model in fixed.models if model.model_id == "company_financials")
    assert cf["earnings_price"]["unavailable_reason"] == REASON
    for key in ("shares_ex_treasury", "market_cap_yen", "earnings_yield"):
        assert cf["earnings_price"][key] is None
    assert cf["capital_action_evidence"][0]["issued_shares"] == 130
    assert fixed.model_dump_json() == before
