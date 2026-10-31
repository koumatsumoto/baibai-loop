import json
from dataclasses import asdict, replace
from datetime import date

import pytest

from baibai_engine.machine_analysis.models.company_financials.earnings import (
    earnings_price,
    read_earnings_price,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.primitives.json import canonical_json

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
        disclosed_time="15:30:00",
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
    assert result.profit_components[0].disclosed_time == "15:30:00"
    assert result.profit_period == (date(2025, 4, 1), date(2026, 3, 31))


def test_unknown_statement_basis_keeps_amount_without_claiming_comparable_yield():
    result = observation([replace(annual(), document_type=None)])
    assert result.profit_ttm_yen == 100.0
    assert result.earnings_yield is None
    assert result.unavailable_reason == "unknown_statement_basis"


def test_equal_profit_does_not_resolve_conflicting_statement_bases():
    group = annual()
    standalone = replace(annual(basis="NonConsolidated"), disclosure_number="standalone-same-time")
    first = observation([group, standalone])
    second = observation([standalone, group])
    assert first == second
    assert first.earnings_yield is second.earnings_yield is None
    assert first.unavailable_reason == second.unavailable_reason == "conflicting_statement_basis"


def test_forecast_only_document_does_not_invalidate_actual_statement_basis():
    forecast = replace(
        annual(),
        disclosed_at=date(2026, 8, 1),
        document_type="EarnForecastRevision",
        disclosure_number="forecast-only",
        profit=None,
        sales=None,
        shares_outstanding=None,
        treasury_shares=None,
        forecast_profit=900.0,
    )
    result = observation([annual(), forecast])
    assert result.profit_ttm_yen == 100.0
    assert result.earnings_yield == 0.125
    assert result.unavailable_reason is None


def test_same_time_actual_dividend_notice_keeps_statement_profit_and_yield():
    statement = annual()
    dividend = replace(
        statement,
        disclosure_number="same-time-dividend",
        document_type="DividendForecastRevision",
        profit=None,
        sales=None,
        shares_outstanding=None,
        treasury_shares=None,
        dps_actual_annual=5.0,
    )
    result = observation([statement, dividend])
    assert result.profit_ttm_yen == 100.0
    assert result.earnings_yield == 0.125


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
        cumulative_eps=999.0,
        disclosure_number="q1-2026",
    )
    current = replace(
        prior,
        disclosed_at=date(2026, 8, 10),
        fiscal_year_end=date(2027, 3, 31),
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        profit=40.0,
        cumulative_eps=-999.0,
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
    assert result.profit_period == (date(2025, 7, 1), date(2026, 6, 30))
    saved = json.loads(canonical_json(asdict(result)))
    restored = read_earnings_price(saved)
    assert restored == result
    assert restored.profit_period == result.profit_period
    assert "profit_period" not in asdict(restored)
    assert canonical_json(asdict(restored)) == canonical_json(saved)


def test_accounting_transition_keeps_price_and_shares_without_a_false_ttm_yield():
    prior = replace(
        annual(20.0),
        fiscal_period="1Q",
        disclosed_at=date(2025, 8, 10),
        period_end=date(2025, 6, 30),
        disclosure_number="prior-q1",
    )
    current = replace(
        prior,
        disclosed_at=date(2026, 8, 10),
        fiscal_year_end=date(2027, 3, 31),
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        document_type="1QFinancialStatements_Consolidated_IFRS",
        disclosure_number="current-q1",
        profit=40.0,
    )
    result = observation([prior, annual(), current])
    assert result.market_cap_yen == 800.0
    assert result.shares_ex_treasury == 80.0
    assert result.profit_ttm_yen is None
    assert result.earnings_yield is None


def test_old_saved_earnings_payload_remains_readable_without_a_new_default_on_disk():
    saved = json.loads(canonical_json(asdict(observation([annual()]))))
    saved.pop("accounting_standard")
    before = canonical_json(saved)
    assert read_earnings_price(saved).profit_ttm_yen == 100.0
    assert canonical_json(saved) == before


@pytest.mark.parametrize(
    ("shape", "expected"),
    [
        ("empty", (None, None)),
        ("missing_start", (None, date(2026, 3, 31))),
        ("two_positive", (None, None)),
        ("missing_positive_end", (None, None)),
        ("missing_negative_end", (None, None)),
    ],
)
def test_profit_period_does_not_invent_missing_component_dates(shape, expected):
    result = observation([annual()])
    part = result.profit_components[0]
    components = {
        "empty": (),
        "missing_start": (replace(part, period_start=None),),
        "two_positive": (part, part),
        "missing_positive_end": (
            part,
            replace(part, period_end=None),
            replace(part, coefficient=-1),
        ),
        "missing_negative_end": (part, part, replace(part, coefficient=-1, period_end=None)),
    }[shape]
    assert replace(result, profit_components=components).profit_period == expected


def test_missing_treasury_shares_is_not_assumed_zero():
    result = observation([replace(annual(), treasury_shares=None)])
    assert result.market_cap_yen is None
    assert result.earnings_yield is None
    assert result.profit_ttm_yen == 100.0


def test_future_disclosure_does_not_enter_fixed_asof():
    future = replace(annual(900.0), disclosed_at=date(2026, 9, 1))
    assert observation([annual(), future]) == observation([annual()])


def test_eleven_month_fiscal_year_is_not_an_annual_profit_observation():
    short_year = replace(annual(), period_end=date(2026, 2, 28), fiscal_year_end=date(2026, 2, 28))
    result = observation([short_year])
    assert result.profit_ttm_yen is None
    assert result.earnings_yield is None


def test_fiscal_year_change_cannot_make_eleven_months_into_ttm():
    prior_half = replace(
        annual(),
        period_end=date(2025, 9, 30),
        disclosed_at=date(2025, 11, 15),
        fiscal_period="2Q",
        profit=20.0,
    )
    short_year = replace(annual(), period_end=date(2026, 2, 28), fiscal_year_end=date(2026, 2, 28))
    current_half = replace(
        annual(),
        period_start=date(2026, 3, 1),
        period_end=date(2026, 8, 31),
        fiscal_year_end=date(2027, 2, 28),
        disclosed_at=date(2026, 10, 15),
        fiscal_period="2Q",
        profit=40.0,
    )
    result = earnings_price(
        [prior_half, short_year, current_half],
        adjusted_price=PriceBar(date(2026, 10, 23), 10.0, None),
        adjustment_events=(),
        as_of=date(2026, 10, 23),
        ttm_rules=RULES,
    )
    assert result.profit_ttm_yen is None
    assert result.earnings_yield is None


def test_unknown_disclosure_time_is_preserved():
    result = observation([replace(annual(), disclosed_time=None)])
    assert result.profit_components[0].disclosed_time is None
    assert result.earnings_yield == 0.125
