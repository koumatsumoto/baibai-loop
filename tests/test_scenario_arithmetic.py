from __future__ import annotations

from decimal import Decimal

import pytest
from tools.scenario_arithmetic import build_claims, parse_scenario, required_return_price

from baibai_engine.research.thesis import ScenarioEstimate, _compare_claims, _recalculate_scenario

_ENTRY = 1217.0
_EARNINGS = 8_000_000_000.0
_SHARES = 87_870_663.0


def _scenario_estimate(spec_text: str, claims: dict[str, float]) -> ScenarioEstimate:
    spec = parse_scenario(spec_text)
    return ScenarioEstimate(
        name=spec.name,
        horizon_years=spec.horizon_years,
        as_of="2026-08-04",
        assumption="parity fixture",
        annual_earnings_growth_pct=spec.annual_earnings_growth_pct,
        annual_share_count_change_pct=spec.annual_share_count_change_pct,
        starting_earnings_yen=Decimal(str(_EARNINGS)),
        starting_earnings_fact_id="earnings",
        starting_share_count=Decimal(str(_SHARES)),
        starting_share_count_fact_id="shares",
        terminal_valuation_multiple=Decimal(str(spec.terminal_valuation_multiple)),
        cumulative_dividend_per_share_yen=Decimal(str(spec.cumulative_dividend_per_share_yen)),
        terminal_price_includes_dividends=False,
        earnings_basis="net_income_attributable_to_owners",
        claimed_terminal_earnings_yen=Decimal(str(claims["claimed_terminal_earnings_yen"])),
        claimed_terminal_share_count=Decimal(str(claims["claimed_terminal_share_count"])),
        claimed_terminal_price_yen=Decimal(str(claims["claimed_terminal_price_yen"])),
        claimed_total_return_cagr_pct=claims["claimed_total_return_cagr_pct"],
        model_version="scenario-v1",
        unit="JPY_per_share_total_return",
        source_ids=("ir",),
    )


@pytest.mark.parametrize(
    "spec_text",
    [
        "bear:3:-2.0:-1.0:10.0:87",
        "base:3:3.0:-1.2:12.0:92",
        "bull:3:7.0:-2.0:14.0:100",
        "bear:5:-2.0:-1.0:10.0:145",
        "base:5:3.0:-1.2:12.0:155",
        "bull:5:7.0:-2.0:14.0:175",
    ],
)
def test_tool_claims_are_accepted_by_the_thesis_claim_comparison(spec_text: str) -> None:
    spec = parse_scenario(spec_text)
    claims = build_claims(
        [spec],
        entry_price_yen=_ENTRY,
        starting_earnings_yen=_EARNINGS,
        starting_share_count=_SHARES,
    )[f"{spec.horizon_years}y/{spec.name}"]

    estimate = _scenario_estimate(spec_text, claims)
    errors: list[str] = []
    _compare_claims(
        estimate, _recalculate_scenario(estimate, entry_price=Decimal(str(_ENTRY))), errors
    )

    assert errors == []


def test_a_hand_edited_claim_is_rejected_by_the_same_comparison() -> None:
    spec_text = "base:5:3.0:-1.2:12.0:155"
    spec = parse_scenario(spec_text)
    claims = build_claims(
        [spec],
        entry_price_yen=_ENTRY,
        starting_earnings_yen=_EARNINGS,
        starting_share_count=_SHARES,
    )[f"{spec.horizon_years}y/{spec.name}"]
    claims["claimed_total_return_cagr_pct"] += 1.0

    estimate = _scenario_estimate(spec_text, claims)
    errors: list[str] = []
    _compare_claims(
        estimate, _recalculate_scenario(estimate, entry_price=Decimal(str(_ENTRY))), errors
    )

    assert any("total-return CAGR mismatch" in error for error in errors)


def test_required_return_price_discounts_the_five_year_base_terminal_value() -> None:
    specs = [parse_scenario("base:5:3.0:-1.2:12.0:155")]
    claims = build_claims(
        specs,
        entry_price_yen=_ENTRY,
        starting_earnings_yen=_EARNINGS,
        starting_share_count=_SHARES,
    )

    price = required_return_price(claims, specs, required_cagr_pct=8.5)

    assert price is not None
    terminal = claims["5y/base"]["claimed_terminal_price_yen"]
    assert price == pytest.approx((terminal + 155) / 1.085**5, abs=1e-4)


def test_required_return_price_is_absent_without_a_five_year_base_scenario() -> None:
    specs = [parse_scenario("base:3:3.0:-1.2:12.0:92")]
    claims = build_claims(
        specs,
        entry_price_yen=_ENTRY,
        starting_earnings_yen=_EARNINGS,
        starting_share_count=_SHARES,
    )

    assert required_return_price(claims, specs, required_cagr_pct=8.5) is None


@pytest.mark.parametrize(
    ("spec_text", "message"),
    [
        ("base:5:3.0:-1.2:12.0", "scenario must be"),
        ("middle:5:3.0:-1.2:12.0:155", "name must be bear/base/bull"),
        ("base:4:3.0:-1.2:12.0:155", "horizon must be 3 or 5"),
    ],
)
def test_scenario_specification_rejects_malformed_input(spec_text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_scenario(spec_text)
