from dataclasses import replace
from datetime import date

import pytest

from baibai_engine.machine_analysis.company_valuation.earnings import earnings_price
from baibai_engine.machine_analysis.company_valuation.metrics.models import TTMRules
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.price_models import PriceBar

ASOF = date(2026, 8, 31)
RULES = TTMRules(
    full_year_min_days=330,
    full_year_max_days=370,
    period_length_tolerance_ratio=0.1,
    period_end_tolerance_days=45,
)


def annual(profit=100.0, basis="Consolidated"):
    return JQuantsFinancialSummary(
        ticker="1001",
        disclosed_at=date(2026, 5, 15),
        fiscal_period="FY",
        fiscal_year_end=date(2026, 3, 31),
        period_start=date(2025, 4, 1),
        period_end=date(2026, 3, 31),
        document_type=f"FYFinancialStatements_{basis}_JP",
        disclosure_number="fy-2026",
        profit=profit,
        sales=1000.0,
        shares_outstanding=100.0,
        treasury_shares=20.0,
    )


def observation(rows):
    return earnings_price(
        rows,
        adjusted_price=PriceBar(ASOF, 10.0, None),
        adjustment_events=(),
        as_of=ASOF,
        ttm_rules=RULES,
    )


@pytest.mark.parametrize("profit", [-100.0, 0.0, 100.0])
@pytest.mark.parametrize("basis", ["Consolidated", "NonConsolidated"])
def test_signed_profit_and_standalone_companies_remain_representable(profit, basis):
    result = observation([annual(profit, basis)])
    assert result.profit_ttm_yen == profit
    assert result.market_cap_yen == 800.0
    assert result.earnings_yield == profit / 800.0
    assert result.unavailable_reason is None
    assert result.statement_basis == basis.lower()
    assert result.profit_components[0].disclosure_number == "fy-2026"


def test_unknown_statement_basis_keeps_amount_without_claiming_comparable_yield():
    result = observation([replace(annual(), document_type=None)])
    assert result.profit_ttm_yen == 100.0
    assert result.earnings_yield is None
    assert result.unavailable_reason == "unknown_statement_basis"


def test_equal_profit_does_not_resolve_conflicting_statement_bases():
    group = annual()
    standalone = replace(
        annual(basis="NonConsolidated"), disclosure_number="standalone-same-time"
    )
    first = observation([group, standalone])
    second = observation([standalone, group])
    assert first == second
    assert first.earnings_yield is second.earnings_yield is None
    assert first.unavailable_reason == second.unavailable_reason == "conflicting_statement_basis"


def test_latest_quarter_missing_profit_does_not_revive_old_full_year():
    current = replace(
        annual(),
        disclosed_at=date(2026, 8, 10),
        fiscal_period="1Q",
        fiscal_year_end=date(2027, 3, 31),
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        profit=None,
        sales=300.0,
        disclosure_number="q1-2027",
    )
    result = observation([annual(), current])
    assert result.profit_ttm_yen is None
    assert result.earnings_yield is None
    assert result.unavailable_reason == "unavailable_annual_profit"


def test_ttm_keeps_actual_operands_instead_of_adding_reported_eps():
    prior = replace(
        annual(),
        disclosed_at=date(2025, 8, 10),
        fiscal_period="1Q",
        period_end=date(2025, 6, 30),
        profit=20.0,
        eps_ttm=999.0,
        disclosure_number="q1-2026",
    )
    current = replace(
        prior,
        disclosed_at=date(2026, 8, 10),
        fiscal_year_end=date(2027, 3, 31),
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        profit=40.0,
        eps_ttm=-999.0,
        disclosure_number="q1-2027",
    )
    result = observation([prior, annual(), current])
    assert result.profit_ttm_yen == 120.0
    assert result.earnings_yield == 0.15
    assert [(part.coefficient, part.amount_yen) for part in result.profit_components] == [
        (1, 40.0),
        (1, 100.0),
        (-1, 20.0),
    ]


def test_missing_treasury_shares_is_not_assumed_zero():
    result = observation([replace(annual(), treasury_shares=None)])
    assert result.market_cap_yen is None
    assert result.earnings_yield is None
    assert result.profit_ttm_yen == 100.0


def test_future_disclosure_does_not_enter_fixed_asof():
    future = replace(annual(900.0), disclosed_at=date(2026, 9, 1))
    assert observation([annual(), future]) == observation([annual()])
