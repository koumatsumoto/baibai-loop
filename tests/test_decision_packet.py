from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
import yaml

from baibai_engine.foundation.yaml_io import safe_load
from baibai_engine.research.decision_cli import main as decision_main
from baibai_engine.research.decision_packet import (
    DecisionPacketDocument,
    DecisionPacketError,
    DecisionPacketResult,
    IndependentReview,
    _round_payload_decimal,
    decision_packet_core_hash,
    evaluate_decision_packet,
    load_decision_packet,
    load_independent_review,
    result_to_payload,
)

ROOT = Path(__file__).parents[1]
FIXTURE = ROOT / "tests/fixtures/decision-packet/2331-decision.yaml"
REVIEW_FIXTURE = ROOT / "tests/fixtures/decision-packet/2331-decision-review.yaml"


def _raw() -> dict[str, object]:
    raw = safe_load(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _document(raw: dict[str, object] | None = None) -> DecisionPacketDocument:
    return DecisionPacketDocument.model_validate(raw or _raw())


def _review_raw() -> dict[str, object]:
    raw = safe_load(REVIEW_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _bind_review(
    raw: dict[str, object], review_raw: dict[str, object] | None = None
) -> tuple[DecisionPacketDocument, IndependentReview]:
    document = _document(raw)
    review = review_raw or _review_raw()
    review["reviewed_packet_sha256"] = decision_packet_core_hash(document)
    return document, IndependentReview.model_validate(review)


def _evaluate(
    raw: dict[str, object],
    review_raw: dict[str, object] | None = None,
    *,
    now: datetime | None = None,
) -> DecisionPacketResult:
    document, review = _bind_review(raw, review_raw)
    return evaluate_decision_packet(document, review=review, now=now)


def _five_year_base_raw(raw: dict[str, object]) -> dict[str, object]:
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    scenarios = estimates["scenarios"]
    assert isinstance(scenarios, list)
    scenario = next(
        item for item in scenarios if item["horizon_years"] == 5 and item["name"] == "base"
    )
    assert isinstance(scenario, dict)
    return scenario


def _screening_estimate(*, fair_value_anchor_yen: object = 1300) -> dict[str, object]:
    return {
        "origin": "estimate",
        "model_version": "screening-estimate-v1",
        "as_of": "2026-07-03",
        "expected_return_annual_ratio": 0.095,
        "expected_return_unit": "annual_ratio",
        "fair_value_anchor_yen": fair_value_anchor_yen,
        "fair_value_unit": "JPY_per_share",
        "assumptions": "Conservative minimum of the available screening FV anchors.",
        "source_ids": ["internal-screen"],
    }


def _screening_fv_bridge() -> dict[str, object]:
    return {
        "primary_driver": "earnings_normalization",
        "note": "Primary-source research uses normalized owner earnings.",
    }


def test_golden_packet_is_ready_with_explicit_evidence_warning() -> None:
    result = evaluate_decision_packet(
        load_decision_packet(FIXTURE), review=load_independent_review(REVIEW_FIXTURE)
    )

    assert result.packet_status == "ready_with_warnings"
    assert result.decision_readiness == "ready"
    assert result.errors == ()
    assert result.warnings == ("permanent-loss evidence incomplete: ['customer_concentration']",)
    assert result.screening_fv_revision_pct is None
    assert (
        result.packet_sha256 == "88b7d6c21b7fd2578709ba1c52fa7472717240455d0e75cc2008444470b1131b"
    )
    assert [(item.horizon_years, item.name) for item in result.scenarios] == [
        (3, "bear"),
        (3, "base"),
        (3, "bull"),
        (5, "bear"),
        (5, "base"),
        (5, "bull"),
    ]
    base_five = next(
        item for item in result.scenarios if item.horizon_years == 5 and item.name == "base"
    )
    assert base_five.terminal_share_count == 97_524_875.3122
    assert base_five.terminal_price_yen == 1439.5401
    assert base_five.total_return_cagr_pct == 9.57
    break_even = result.five_year_base_break_even
    assert break_even is not None
    assert break_even.break_even_terminal_valuation_multiple == pytest.approx(
        Decimal("1.0135050773543111")
    )
    assert break_even.break_even_annual_earnings_growth_pct == pytest.approx(
        Decimal("3.2942026976003037")
    )
    assert result_to_payload(result)["five_year_base_break_even"] == {
        "required_total_value_yen": 1516.3466,
        "required_total_return_cagr_pct": 8.0,
        "base_terminal_valuation_multiple": 1.1,
        "break_even_terminal_valuation_multiple": 1.0135,
        "terminal_multiple_downside_buffer": 0.0865,
        "terminal_multiple_status": "within_model_bounds",
        "base_annual_earnings_growth_pct": 5.0,
        "break_even_annual_earnings_growth_pct": 3.2942,
        "earnings_growth_downside_buffer_pct_points": 1.7058,
        "earnings_growth_status": "within_model_bounds",
        "observed_trailing_multiple_status": "resolved",
        "observed_trailing_multiple_fact_id": "trailing-per",
        "observed_trailing_multiple": 10.32,
        "base_terminal_multiple_minus_observed": -9.22,
        "base_terminal_multiple_premium_pct": -89.3411,
    }


def test_optional_screening_fields_preserve_legacy_hash_and_bind_new_values() -> None:
    absent = _raw()
    explicit_null = copy.deepcopy(absent)
    null_snapshot = explicit_null["input_snapshot"]
    null_estimates = explicit_null["estimates"]
    assert isinstance(null_snapshot, dict)
    assert isinstance(null_estimates, dict)
    null_snapshot["screening_estimate"] = None
    null_estimates["screening_fv_bridge"] = None

    legacy_hash = decision_packet_core_hash(_document(absent))
    assert decision_packet_core_hash(_document(explicit_null)) == legacy_hash

    bridged = copy.deepcopy(absent)
    snapshot = bridged["input_snapshot"]
    estimates = bridged["estimates"]
    assert isinstance(snapshot, dict)
    assert isinstance(estimates, dict)
    snapshot["screening_estimate"] = _screening_estimate()
    estimates["screening_fv_bridge"] = _screening_fv_bridge()
    bridged_hash = decision_packet_core_hash(_document(bridged))
    assert bridged_hash != legacy_hash

    changed = copy.deepcopy(bridged)
    changed_estimates = changed["estimates"]
    assert isinstance(changed_estimates, dict)
    changed_bridge = changed_estimates["screening_fv_bridge"]
    assert isinstance(changed_bridge, dict)
    changed_bridge["primary_driver"] = "growth"
    assert decision_packet_core_hash(_document(changed)) != bridged_hash


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("primary_driver", "pricing", "primary_driver"),
        ("note", "", "at least 1 character"),
        ("note", "   ", "match pattern"),
        ("note", "first line\nsecond line\nthird line", "match pattern"),
    ],
)
def test_screening_fv_bridge_rejects_invalid_contract_values(
    field: str, value: object, message: str
) -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    estimates = raw["estimates"]
    assert isinstance(snapshot, dict)
    assert isinstance(estimates, dict)
    snapshot["screening_estimate"] = _screening_estimate()
    bridge = _screening_fv_bridge()
    bridge[field] = value
    estimates["screening_fv_bridge"] = bridge

    with pytest.raises(ValueError, match=message):
        _document(raw)


def test_screening_fv_bridge_accepts_two_physical_lines() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    estimates = raw["estimates"]
    assert isinstance(snapshot, dict)
    assert isinstance(estimates, dict)
    snapshot["screening_estimate"] = _screening_estimate()
    estimates["screening_fv_bridge"] = {
        "primary_driver": "earnings_normalization",
        "note": "Primary-source research normalizes earnings.\nThe second line states the key limitation.",
    }

    assert _document(raw).estimates.screening_fv_bridge is not None


def test_screening_anchor_without_bridge_is_a_non_blocking_warning() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    risks = raw["permanent_loss_risks"]
    judgment = raw["judgment"]
    assert isinstance(snapshot, dict)
    assert isinstance(risks, list)
    assert isinstance(judgment, dict)
    snapshot["screening_estimate"] = _screening_estimate()
    for risk in risks:
        risk["assessment"] = "acceptable"
        risk["evidence_status"] = "verified"
    judgment["permanent_loss_conclusion"] = "acceptable"
    judgment["sizing_action"] = "normal"
    raw.pop("human_evidence_override", None)

    result = _evaluate(raw)

    assert result.errors == ()
    assert result.decision_readiness == "ready"
    assert result.warnings == ("screening fair-value anchor has no screening_fv_bridge",)


@pytest.mark.parametrize("baseline", [None, _screening_estimate(fair_value_anchor_yen=None)])
def test_screening_fv_bridge_requires_a_baseline_anchor(
    baseline: dict[str, object] | None,
) -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    estimates = raw["estimates"]
    assert isinstance(snapshot, dict)
    assert isinstance(estimates, dict)
    snapshot["screening_estimate"] = baseline
    estimates["screening_fv_bridge"] = _screening_fv_bridge()

    result = _evaluate(raw)

    assert any("screening_fv_bridge requires" in error for error in result.errors)


def test_screening_estimate_sources_require_lineage_without_review_coverage() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    screening_estimate = _screening_estimate()
    screening_estimate["source_ids"] = ["missing-screening-source"]
    snapshot["screening_estimate"] = screening_estimate

    result = _evaluate(raw)

    assert any("screening estimate references unknown sources" in error for error in result.errors)
    assert not any("did not check load-bearing sources" in error for error in result.errors)


def test_screening_estimate_requires_packet_as_of_and_local_data_source() -> None:
    stale_raw = _raw()
    stale_snapshot = stale_raw["input_snapshot"]
    assert isinstance(stale_snapshot, dict)
    stale_estimate = _screening_estimate()
    stale_estimate["as_of"] = "2026-07-02"
    stale_snapshot["screening_estimate"] = stale_estimate

    stale_result = _evaluate(stale_raw)

    assert any("screening_estimate.as_of must equal" in error for error in stale_result.errors)

    primary_raw = _raw()
    primary_snapshot = primary_raw["input_snapshot"]
    assert isinstance(primary_snapshot, dict)
    primary_estimate = _screening_estimate()
    primary_estimate["source_ids"] = ["primary-results"]
    primary_snapshot["screening_estimate"] = primary_estimate

    primary_result = _evaluate(primary_raw)

    assert any(
        "screening_estimate requires a local_data source" in error
        for error in primary_result.errors
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_return_annual_ratio", -1.0001),
        ("expected_return_annual_ratio", 10.0001),
        ("fair_value_anchor_yen", 0.0001),
        ("fair_value_anchor_yen", 1_000_000_001),
    ],
)
def test_screening_estimate_rejects_out_of_bounds_values(field: str, value: object) -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    screening_estimate = _screening_estimate()
    screening_estimate[field] = value
    snapshot["screening_estimate"] = screening_estimate

    with pytest.raises(ValueError, match=field):
        _document(raw)


def test_screening_fv_revision_uses_raw_decimal_values() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    estimates = raw["estimates"]
    assert isinstance(snapshot, dict)
    assert isinstance(estimates, dict)
    snapshot["screening_estimate"] = _screening_estimate(fair_value_anchor_yen=1484.5)
    estimates["screening_fv_bridge"] = _screening_fv_bridge()
    estimates["current_fair_value_yen"] = 1481.9088

    result = _evaluate(raw)

    assert result.screening_fv_revision_pct == pytest.approx(Decimal("-0.1745503536544291"))
    assert result_to_payload(result)["screening_fv_revision_pct"] == -0.1746


def test_break_even_values_reproduce_required_return_and_are_monotonic() -> None:
    document = _document()
    result = evaluate_decision_packet(document)
    break_even = result.five_year_base_break_even
    assert break_even is not None
    assert break_even.required_total_value_yen is not None
    assert break_even.break_even_terminal_valuation_multiple is not None
    assert break_even.break_even_annual_earnings_growth_pct is not None
    scenario = next(
        item
        for item in document.estimates.scenarios
        if item.horizon_years == 5 and item.name == "base"
    )

    with localcontext() as context:
        context.prec = 50
        one = Decimal(1)
        hundred = Decimal(100)
        terminal_shares = (
            scenario.starting_share_count
            * (one + Decimal(str(scenario.annual_share_count_change_pct)) / hundred) ** 5
        )
        base_terminal_earnings = (
            scenario.starting_earnings_yen
            * (one + Decimal(str(scenario.annual_earnings_growth_pct)) / hundred) ** 5
        )
        multiple_total = (
            base_terminal_earnings
            / terminal_shares
            * break_even.break_even_terminal_valuation_multiple
            + scenario.cumulative_dividend_per_share_yen
        )
        growth_terminal_earnings = (
            scenario.starting_earnings_yen
            * (one + break_even.break_even_annual_earnings_growth_pct / hundred) ** 5
        )
        growth_total = (
            growth_terminal_earnings / terminal_shares * scenario.terminal_valuation_multiple
            + scenario.cumulative_dividend_per_share_yen
        )

        assert multiple_total == pytest.approx(break_even.required_total_value_yen)
        assert growth_total == pytest.approx(break_even.required_total_value_yen)
        multiple_step = Decimal("0.0001")
        assert (
            base_terminal_earnings
            / terminal_shares
            * (break_even.break_even_terminal_valuation_multiple - multiple_step)
            + scenario.cumulative_dividend_per_share_yen
            < break_even.required_total_value_yen
        )
        assert (
            base_terminal_earnings
            / terminal_shares
            * (break_even.break_even_terminal_valuation_multiple + multiple_step)
            + scenario.cumulative_dividend_per_share_yen
            > break_even.required_total_value_yen
        )
        growth_step = Decimal("0.0001")
        below_growth_earnings = (
            scenario.starting_earnings_yen
            * (one + (break_even.break_even_annual_earnings_growth_pct - growth_step) / hundred)
            ** 5
        )
        above_growth_earnings = (
            scenario.starting_earnings_yen
            * (one + (break_even.break_even_annual_earnings_growth_pct + growth_step) / hundred)
            ** 5
        )
        assert (
            below_growth_earnings / terminal_shares * scenario.terminal_valuation_multiple
            + scenario.cumulative_dividend_per_share_yen
            < break_even.required_total_value_yen
        )
        assert (
            above_growth_earnings / terminal_shares * scenario.terminal_valuation_multiple
            + scenario.cumulative_dividend_per_share_yen
            > break_even.required_total_value_yen
        )


def test_break_even_handles_dividends_and_model_bounds() -> None:
    dividend_raw = _raw()
    _five_year_base_raw(dividend_raw)["cumulative_dividend_per_share_yen"] = 2000
    dividend_result = _evaluate(dividend_raw)
    dividend_break_even = dividend_result.five_year_base_break_even
    assert dividend_break_even is not None
    assert dividend_break_even.terminal_multiple_status == "dividends_alone_sufficient"
    assert dividend_break_even.earnings_growth_status == "dividends_alone_sufficient"
    assert dividend_break_even.break_even_terminal_valuation_multiple is None
    assert dividend_break_even.break_even_annual_earnings_growth_pct is None
    assert dividend_break_even.terminal_multiple_downside_buffer is None
    assert dividend_break_even.earnings_growth_downside_buffer_pct_points is None

    above_raw = _raw()
    above_base = _five_year_base_raw(above_raw)
    above_base["annual_earnings_growth_pct"] = -50
    above_base["annual_share_count_change_pct"] = 20
    above_base["terminal_valuation_multiple"] = 0.0001
    above_break_even = _evaluate(above_raw).five_year_base_break_even
    assert above_break_even is not None
    assert above_break_even.terminal_multiple_status == "above_model_max"
    assert above_break_even.earnings_growth_status == "above_model_max"

    below_raw = _raw()
    _five_year_base_raw(below_raw)["terminal_valuation_multiple"] = 100
    below_break_even = _evaluate(below_raw).five_year_base_break_even
    assert below_break_even is not None
    assert below_break_even.earnings_growth_status == "below_model_min"


def test_break_even_payload_rounding_handles_large_finite_values() -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    estimates["entry_price_basis_yen"] = 1_000_000_000
    estimates["required_5y_base_cagr_pct"] = 100
    base = _five_year_base_raw(raw)
    base["starting_earnings_yen"] = 0.0001
    base["starting_share_count"] = 10_000_000_000_000
    base["annual_share_count_change_pct"] = 20

    result = _evaluate(raw)
    break_even = result.five_year_base_break_even
    assert break_even is not None
    assert break_even.terminal_multiple_status == "above_model_max"
    payload = result_to_payload(result)["five_year_base_break_even"]
    assert isinstance(payload, dict)
    payload_multiple = payload["break_even_terminal_valuation_multiple"]
    assert isinstance(payload_multiple, float)
    assert payload_multiple > 6e27
    assert (
        _round_payload_decimal(Decimal("99999999999999999999999999999999999999999999999999.99995"))
        == 1e50
    )


def test_observed_trailing_multiple_requires_one_valid_named_local_anchor() -> None:
    missing_raw = _raw()
    snapshot = missing_raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    trailing = next(item for item in facts if item["fact_id"] == "trailing-per")
    trailing["fact_id"] = "unrelated-valuation-metric"
    missing = _evaluate(missing_raw).five_year_base_break_even
    assert missing is not None
    assert missing.observed_trailing_multiple_status == "missing"
    assert missing.observed_trailing_multiple is None

    ambiguous_raw = _raw()
    ambiguous_snapshot = ambiguous_raw["input_snapshot"]
    assert isinstance(ambiguous_snapshot, dict)
    ambiguous_facts = ambiguous_snapshot["facts"]
    assert isinstance(ambiguous_facts, list)
    duplicate = copy.deepcopy(
        next(item for item in ambiguous_facts if item["fact_id"] == "trailing-per")
    )
    duplicate["fact_id"] = "trailing-per-screening"
    ambiguous_facts.append(duplicate)
    ambiguous = _evaluate(ambiguous_raw).five_year_base_break_even
    assert ambiguous is not None
    assert ambiguous.observed_trailing_multiple_status == "ambiguous"

    invalid_raw = _raw()
    invalid_snapshot = invalid_raw["input_snapshot"]
    assert isinstance(invalid_snapshot, dict)
    invalid_facts = invalid_snapshot["facts"]
    assert isinstance(invalid_facts, list)
    invalid_fact = next(item for item in invalid_facts if item["fact_id"] == "trailing-per")
    invalid_fact["source_ids"] = ["primary-results"]
    invalid = _evaluate(invalid_raw).five_year_base_break_even
    assert invalid is not None
    assert invalid.observed_trailing_multiple_status == "invalid"
    assert invalid.observed_trailing_multiple_fact_id == "trailing-per"
    assert invalid.observed_trailing_multiple is None

    duplicate_source_raw = _raw()
    duplicate_snapshot = duplicate_source_raw["input_snapshot"]
    assert isinstance(duplicate_snapshot, dict)
    duplicate_sources = duplicate_snapshot["sources"]
    assert isinstance(duplicate_sources, list)
    local_source = next(
        item for item in duplicate_sources if item["source_id"] == "internal-screen"
    )
    duplicate_source = copy.deepcopy(local_source)
    duplicate_source["source_tier"] = "primary"
    duplicate_source["ref"] = "https://example.com/duplicate-source"
    duplicate_source.pop("provider")
    duplicate_source.pop("dataset")
    duplicate_sources.append(duplicate_source)
    duplicate_anchor = _evaluate(duplicate_source_raw).five_year_base_break_even
    assert duplicate_anchor is not None
    assert duplicate_anchor.observed_trailing_multiple_status == "invalid"
    assert duplicate_anchor.observed_trailing_multiple is None

    fcfe_raw = _raw()
    _five_year_base_raw(fcfe_raw)["earnings_basis"] = "fcfe"
    not_applicable = _evaluate(fcfe_raw).five_year_base_break_even
    assert not_applicable is not None
    assert not_applicable.observed_trailing_multiple_status == "not_applicable"


def test_packet_requires_input_snapshot() -> None:
    raw = _raw()
    del raw["input_snapshot"]

    with pytest.raises(ValueError, match="input_snapshot"):
        _document(raw)


@pytest.mark.parametrize("value", [None, 0, -1])
def test_packet_requires_an_explicit_positive_5y_base_return(value: object) -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    if value is None:
        del estimates["required_5y_base_cagr_pct"]
    else:
        estimates["required_5y_base_cagr_pct"] = value

    with pytest.raises(ValueError, match="required_5y_base_cagr_pct"):
        _document(raw)


def test_ai_value_capture_rejects_uncaptured_material_weight() -> None:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    ai_value_capture = judgment["ai_value_capture"]
    assert isinstance(ai_value_capture, dict)
    ai_value_capture["value_capture_conclusion"] = "not_captured"

    with pytest.raises(ValueError, match="cannot carry decision weight"):
        _document(raw)


def test_ai_value_capture_rejects_not_material_role() -> None:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    ai_value_capture = judgment["ai_value_capture"]
    assert isinstance(ai_value_capture, dict)
    ai_value_capture["assessment_status"] = "not_material"

    with pytest.raises(ValueError, match="requires no roles and none weight"):
        _document(raw)


def test_disrupted_ai_role_requires_structural_decline_risk_and_shared_source() -> None:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    ai_value_capture = judgment["ai_value_capture"]
    assert isinstance(ai_value_capture, dict)
    ai_value_capture["roles"] = ["disrupted"]
    risks = raw["permanent_loss_risks"]
    assert isinstance(risks, list)
    structural_decline = next(item for item in risks if item["axis"] == "structural_decline")
    assert isinstance(structural_decline, dict)
    structural_decline["assessment"] = "acceptable"

    result = _evaluate(raw)

    assert "disrupted AI role requires adverse or unknown structural_decline risk" in result.errors


def test_ai_value_capture_requires_known_source() -> None:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    ai_value_capture = judgment["ai_value_capture"]
    assert isinstance(ai_value_capture, dict)
    ai_value_capture["source_ids"] = ["missing-source"]

    result = _evaluate(raw)

    assert any("AI value capture references unknown sources" in error for error in result.errors)


def test_ai_value_capture_requires_a_source() -> None:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    ai_value_capture = judgment["ai_value_capture"]
    assert isinstance(ai_value_capture, dict)
    ai_value_capture["source_ids"] = []

    with pytest.raises(ValueError, match="at least 1 item"):
        _document(raw)


def test_source_ticker_must_match_snapshot_ticker() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    assert isinstance(sources, list)
    sources[0]["ticker"] = "2332"

    result = _evaluate(raw)

    assert any("ticker does not match input_snapshot" in error for error in result.errors)


def test_snapshot_future_as_of_is_rejected() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    snapshot["as_of"] = "2099-01-01"

    result = _evaluate(raw)

    assert "packet as_of cannot be in the future" in result.errors


def test_future_source_retrieval_is_rejected() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    assert isinstance(sources, list)
    sources[0]["retrieved_at"] = "2099-01-01T00:00:00+09:00"

    result = _evaluate(raw)

    assert any("retrieval is future-dated" in error for error in result.errors)


def test_source_retrieved_after_proposal_is_rejected() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    assert isinstance(sources, list)
    sources[0]["retrieved_at"] = "2026-07-03T09:00:01+09:00"

    result = _evaluate(raw)

    assert any("retrieved after the AI proposal" in error for error in result.errors)


def test_market_price_unit_is_checked() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[0]["unit"] = "JPY"

    result = _evaluate(raw)

    assert "market_price fact unit must be JPY_per_share" in result.errors


def test_observed_entry_price_must_equal_snapshot_market_price() -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    estimates["entry_price_basis_yen"] = 1031

    result = _evaluate(raw)

    assert "observed_market_price entry basis must equal snapshot market price" in result.errors


def test_valuation_fact_must_be_numeric() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[1]["value"] = "cheap"

    result = _evaluate(raw)

    assert "valuation fact trailing-per must be numeric" in result.errors


def test_snapshot_fact_requires_known_source() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[0]["source_ids"] = ["missing-source"]

    result = _evaluate(raw)

    assert any("references unknown sources" in error for error in result.errors)


def test_snapshot_requires_exactly_one_market_price() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[0]["fact_kind"] = "other"
    facts[0].pop("observed_at")
    facts[0].pop("price_basis")

    result = _evaluate(raw)

    assert "input_snapshot requires exactly one market_price fact" in result.errors


def test_snapshot_rejects_multiple_market_prices() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    duplicate = copy.deepcopy(facts[0])
    duplicate["fact_id"] = "second-market-price"
    facts.append(duplicate)

    result = _evaluate(raw)

    assert "input_snapshot requires exactly one market_price fact" in result.errors


def test_snapshot_requires_valuation_fact() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[1]["fact_kind"] = "other"

    result = _evaluate(raw)

    assert "input_snapshot requires at least one valuation_metric fact" in result.errors


def test_fixture_lineage_is_self_contained_for_clean_checkout() -> None:
    document = load_decision_packet(FIXTURE)

    assert all(
        source.ref is None
        for source in document.input_snapshot.sources
        if source.source_tier == "local_data"
    )
    assert all(
        source.provider and source.dataset
        for source in document.input_snapshot.sources
        if source.source_tier == "local_data"
    )
    assert "candidate_ref" not in FIXTURE.read_text(encoding="utf-8")


def test_missing_risk_axis_makes_packet_incomplete() -> None:
    raw = _raw()
    risks = raw["permanent_loss_risks"]
    assert isinstance(risks, list)
    risks.pop()

    result = _evaluate(raw)

    assert result.packet_status == "incomplete"
    assert any("missing permanent-loss risk axis" in error for error in result.errors)


def test_missing_source_reference_makes_packet_incomplete() -> None:
    raw = _raw()
    risks = raw["permanent_loss_risks"]
    assert isinstance(risks, list)
    risks[0]["source_ids"] = []

    result = _evaluate(raw)

    assert any("risk funding_liquidity requires source_ids" in error for error in result.errors)


def test_future_as_of_makes_packet_incomplete() -> None:
    raw = _raw()
    risks = raw["permanent_loss_risks"]
    assert isinstance(risks, list)
    risks[0]["as_of"] = "2026-07-04"

    result = _evaluate(raw)

    assert any(
        "risk funding_liquidity as_of is after packet as_of" in error for error in result.errors
    )


def test_adverse_risk_cannot_be_called_acceptable() -> None:
    raw = _raw()
    risks = raw["permanent_loss_risks"]
    judgment = raw["judgment"]
    assert isinstance(risks, list)
    assert isinstance(judgment, dict)
    risks[0]["assessment"] = "adverse"
    judgment["permanent_loss_conclusion"] = "acceptable"

    result = _evaluate(raw)

    assert any("contradicts permanent-loss risk axes" in error for error in result.errors)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("claimed_terminal_share_count", "terminal share count mismatch"),
        ("claimed_terminal_price_yen", "terminal price mismatch"),
        ("claimed_total_return_cagr_pct", "total-return CAGR mismatch"),
    ],
)
def test_scenario_arithmetic_is_recalculated(field: str, message: str) -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    scenarios = estimates["scenarios"]
    assert isinstance(scenarios, list)
    scenarios[0][field] = 999

    result = _evaluate(raw)

    assert any(message in error for error in result.errors)


def test_scenario_starting_values_must_match_observed_facts() -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    scenarios = estimates["scenarios"]
    assert isinstance(scenarios, list)
    scenarios[0]["starting_earnings_yen"] = 99_000_000_000

    result = _evaluate(raw)

    assert any("does not match observed fact normalized-profit" in error for error in result.errors)


def test_scenario_fact_kind_must_match_earnings_basis() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[2]["fact_kind"] = "other"

    result = _evaluate(raw)

    assert any("fact kind must be net_income" in error for error in result.errors)


def test_derived_metric_is_recalculated_from_fact_ids() -> None:
    raw = _raw()
    derived = raw["derived"]
    assert isinstance(derived, dict)
    metrics = derived["metrics"]
    assert isinstance(metrics, list)
    metrics[0]["value"] = 999

    result = _evaluate(raw)

    assert any("metric normalized-eps value mismatch" in error for error in result.errors)


def test_derived_ratio_unit_is_checked() -> None:
    raw = _raw()
    derived = raw["derived"]
    assert isinstance(derived, dict)
    metrics = derived["metrics"]
    assert isinstance(metrics, list)
    metrics[0]["unit"] = "percent"

    result = _evaluate(raw)

    assert any("unit mismatch for ratio inputs" in error for error in result.errors)


def test_dividends_cannot_be_embedded_in_terminal_price() -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    scenarios = estimates["scenarios"]
    assert isinstance(scenarios, list)
    scenarios[0]["terminal_price_includes_dividends"] = True

    with pytest.raises(ValueError, match="Input should be False"):
        _document(raw)


def test_incomplete_evidence_cannot_claim_high_confidence() -> None:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    judgment["confidence"] = "high"

    result = _evaluate(raw)

    assert any("high confidence" in error for error in result.errors)


def test_verified_review_must_check_a_primary_source() -> None:
    raw = _raw()
    review = _review_raw()
    review["checked_source_ids"] = ["internal-screen"]

    result = _evaluate(raw, review)

    assert any("must check a primary source" in error for error in result.errors)


def test_partially_verified_primary_review_requires_override_for_buy() -> None:
    raw = _raw()
    risks = raw["permanent_loss_risks"]
    judgment = raw["judgment"]
    review = _review_raw()
    assert isinstance(risks, list)
    assert isinstance(judgment, dict)
    for risk in risks:
        risk["assessment"] = "acceptable"
        risk["evidence_status"] = "verified"
    judgment["permanent_loss_conclusion"] = "acceptable"
    judgment["sizing_action"] = "normal"
    raw["human_evidence_override"] = None
    review["primary_source_check"] = "partially_verified"

    result = _evaluate(raw, review)

    assert any("without fully verified primary review" in error for error in result.errors)


def test_nonfinite_metric_is_rejected() -> None:
    raw = _raw()
    derived = raw["derived"]
    assert isinstance(derived, dict)
    metrics = derived["metrics"]
    assert isinstance(metrics, list)
    metrics[0]["value"] = float("nan")

    with pytest.raises(ValueError, match="finite number"):
        _document(raw)


def test_buy_with_evidence_gap_requires_reduced_sizing_and_override() -> None:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    judgment["sizing_action"] = "normal"
    raw["human_evidence_override"] = None

    result = _evaluate(raw)

    assert any("requires a human override and reduced sizing" in error for error in result.errors)


def test_future_human_override_cannot_authorize_earlier_judgment() -> None:
    raw = _raw()
    override = raw["human_evidence_override"]
    assert isinstance(override, dict)
    override["approved_at"] = "2026-07-03T12:00:00+09:00"

    result = _evaluate(raw, now=datetime.fromisoformat("2026-07-03T10:30:00+09:00"))

    assert any("requires a human override" in error for error in result.errors)


def test_expired_human_override_cannot_keep_buy_ready() -> None:
    result = _evaluate(_raw(), now=datetime.fromisoformat("2026-08-01T10:00:00+09:00"))

    assert any("requires a human override" in error for error in result.errors)


def test_override_is_invalid_at_exact_expiry() -> None:
    result = _evaluate(_raw(), now=datetime.fromisoformat("2026-07-31T15:30:00+09:00"))

    assert any("requires a human override" in error for error in result.errors)


def test_human_override_is_bound_to_exact_review_artifact() -> None:
    raw = _raw()
    review = _review_raw()
    review["strongest_countercase"] = "A different countercase not accepted by the human."

    result = _evaluate(raw, review)

    assert any("requires a human override" in error for error in result.errors)


@pytest.mark.parametrize("field", ["proposal_sha256", "review_id", "review_sha256"])
def test_human_override_binding_fields_cannot_be_reused(field: str) -> None:
    raw = _raw()
    override = raw["human_evidence_override"]
    assert isinstance(override, dict)
    override[field] = "0" * 64 if field.endswith("sha256") else "different-review"

    result = _evaluate(raw)

    assert any("requires a human override" in error for error in result.errors)


def test_review_must_follow_initial_proposal() -> None:
    raw = _raw()
    review = _review_raw()
    review["reviewed_at"] = "2026-07-03T08:00:00+09:00"

    result = _evaluate(raw, review)

    assert any("cannot predate the AI proposal" in error for error in result.errors)


def test_review_must_cover_every_load_bearing_source() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    facts = snapshot["facts"]
    assert isinstance(sources, list)
    assert isinstance(facts, list)
    sources.append(
        {
            "source_id": "fact-only-source",
            "ticker": "2331",
            "source_tier": "secondary",
            "ref": "https://example.com/fact",
            "retrieved_at": "2026-07-03T08:45:00+09:00",
            "as_of": "2026-06-30",
            "used_for": "scenario starting fact",
        }
    )
    facts[2]["source_ids"] = ["fact-only-source"]
    review = _review_raw()

    result = _evaluate(raw, review)

    assert any("did not check load-bearing sources" in error for error in result.errors)


def test_extreme_growth_is_rejected_before_arithmetic() -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    scenarios = estimates["scenarios"]
    assert isinstance(scenarios, list)
    scenarios[0]["annual_earnings_growth_pct"] = 1e308

    with pytest.raises(ValueError, match="less than or equal to 50"):
        _document(raw)


def test_huge_observed_integer_is_rejected_before_hashing() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    facts = snapshot["facts"]
    assert isinstance(facts, list)
    facts[0]["value"] = 10**10_000

    with pytest.raises(ValueError, match="less than or equal"):
        _document(raw)


def test_stale_source_cannot_support_current_fact() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    assert isinstance(sources, list)
    sources[0]["as_of"] = "2020-01-01"

    result = _evaluate(raw)

    assert any(
        "source primary-results is more than 400 days old" in error for error in result.errors
    )


def test_old_fact_date_cannot_be_hidden_by_matching_old_source_date() -> None:
    raw = _raw()
    snapshot = raw["input_snapshot"]
    assert isinstance(snapshot, dict)
    sources = snapshot["sources"]
    facts = snapshot["facts"]
    assert isinstance(sources, list)
    assert isinstance(facts, list)
    sources.append(
        {
            "source_id": "old-fact-source",
            "ticker": "2331",
            "source_tier": "secondary",
            "ref": "https://example.com/old",
            "retrieved_at": "2026-07-03T08:45:00+09:00",
            "as_of": "2020-01-01",
            "used_for": "stale starting fact",
        }
    )
    facts[2]["as_of"] = "2020-01-01"
    facts[2]["source_ids"] = ["old-fact-source"]
    review = _review_raw()
    checked = review["checked_source_ids"]
    assert isinstance(checked, list)
    checked.append("old-fact-source")

    result = _evaluate(raw, review)

    assert any(
        "fact normalized-profit is more than 400 days older" in error for error in result.errors
    )


def test_verified_adverse_risk_requires_human_override_and_reduced_sizing() -> None:
    raw = _raw()
    risks = raw["permanent_loss_risks"]
    judgment = raw["judgment"]
    assert isinstance(risks, list)
    assert isinstance(judgment, dict)
    risks[0]["assessment"] = "adverse"
    judgment["permanent_loss_conclusion"] = "elevated"
    judgment["sizing_action"] = "normal"
    raw["human_evidence_override"] = None

    result = _evaluate(raw)

    assert any("incomplete or adverse evidence" in error for error in result.errors)


def test_malformed_decimal_is_a_structured_load_error(tmp_path: Path) -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    estimates["entry_price_basis_yen"] = "bad"
    path = tmp_path / "bad-decision.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(DecisionPacketError, match="fixed-point notation"):
        load_decision_packet(path)


def test_scientific_decimal_string_is_rejected_like_public_schema() -> None:
    raw = _raw()
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    estimates["entry_price_basis_yen"] = "1e3"

    with pytest.raises(ValueError, match="fixed-point notation"):
        _document(raw)


def test_future_proposal_is_not_decision_ready() -> None:
    raw = _raw()
    judgment = raw["judgment"]
    assert isinstance(judgment, dict)
    judgment["proposed_at"] = "2026-07-12T11:00:00+09:00"

    # Inject a fixed aware clock earlier than proposed_at so the future-dating
    # gate is exercised deterministically regardless of the wall clock the suite
    # runs under.
    result = _evaluate(raw, now=datetime.fromisoformat("2026-07-12T10:00:00+09:00"))

    assert "proposal cannot be future-dated" in result.errors


def test_buy_candidate_requires_independent_second_pass() -> None:
    raw = _raw()
    raw["independent_review_ref"] = None

    result = evaluate_decision_packet(_document(raw))

    assert result.packet_status == "review_required"
    assert result.errors == ("buy recommendation requires an independent second-pass review",)


def test_changed_second_pass_requires_packet_regeneration() -> None:
    raw = _raw()
    review = _review_raw()
    review["proposal_changed"] = True
    review["change_rationale"] = "Use the lower price ceiling after recalculation."

    result = _evaluate(raw, review)

    assert any("changed the proposal" in error for error in result.errors)


def test_read_only_cli_uses_domain_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "2026-07-03-2331-decision.yaml"
    path.write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (path.parent / "2331-decision-review.yaml").write_text(
        REVIEW_FIXTURE.read_text(encoding="utf-8"), encoding="utf-8"
    )

    assert decision_main([str(path)]) == 0
    output = yaml.safe_load(capsys.readouterr().out)
    assert output["packet_status"] == "ready_with_warnings"
    assert output["decision_readiness"] == "ready"


def test_independent_review_hash_changes_with_initial_proposal() -> None:
    raw = copy.deepcopy(_raw())
    original = decision_packet_core_hash(_document(raw))
    estimates = raw["estimates"]
    assert isinstance(estimates, dict)
    estimates["entry_price_basis_yen"] = 1040

    assert decision_packet_core_hash(_document(raw)) != original
