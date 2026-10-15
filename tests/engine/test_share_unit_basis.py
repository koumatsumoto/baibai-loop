"""Price-adjustment events are not interchangeable with share-unit changes."""

from dataclasses import replace
from datetime import date

import pytest
from tests.engine.screening.metric_fixtures import _summary

from baibai_engine.market.bars import (
    JQuantsAdjustmentFactorEvent,
    asof_basis_prices,
    share_unit_factor,
)
from baibai_engine.screening.calibration.forward import (
    _FYDividendObservation,
    _resolve_total_return,
)
from baibai_engine.valuation.metrics.capital import _normalize_summaries_with_status
from baibai_engine.valuation.metrics.dividends import _resolve_dividend_carry


@pytest.mark.parametrize(
    ("kind", "price_factor", "unit_factor"),
    [("1", 0.5, 0.5), ("2", 2.0, 2.0), ("3", 0.8, 1.0), (None, 0.8, None)],
)
def test_price_adjustment_and_financial_share_units_are_separate(kind, price_factor, unit_factor):
    before = date(2026, 8, 1)
    action_day = date(2026, 9, 1)
    event = JQuantsAdjustmentFactorEvent("1111", action_day, price_factor, kind)
    source = _summary(
        "1111",
        before,
        shares_outstanding=100.0,
        treasury_shares=10.0,
        eps_ttm=20.0,
        dps_actual_annual=8.0,
    )
    source = replace(source, average_shares=90.0)
    assert asof_basis_prices(
        [(before, 100.0)], [(action_day, price_factor)], asof_date=action_day
    ) == [100.0 * price_factor]
    assert share_unit_factor(event) == unit_factor
    result = _normalize_summaries_with_status([source], [event], action_day)
    row = result.summaries[0]
    assert row.sales == source.sales
    assert row.profit == source.profit
    if unit_factor is None:
        assert row.shares_outstanding is None
        assert row.dps_actual_annual is None
        assert result.capital_basis_barrier is not None
    else:
        assert row.shares_outstanding == 100.0 / unit_factor
        assert row.treasury_shares == 10.0 / unit_factor
        assert row.average_shares == 90.0 / unit_factor
        assert row.eps_ttm == 20.0 * unit_factor
        assert row.dps_actual_annual == 8.0 * unit_factor
        assert result.capital_basis_barrier is None


def test_rights_issue_inside_dividend_year_does_not_require_split_payment_details():
    disclosed = date(2026, 5, 15)
    source = _summary(
        "1111",
        disclosed,
        fiscal_period="FY",
        period_start=date(2025, 4, 1),
        period_end=date(2026, 3, 31),
        shares_outstanding=100.0,
        dps_actual_annual=8.0,
    )
    event = JQuantsAdjustmentFactorEvent("1111", date(2026, 2, 1), 0.8, "3")
    normalized = _normalize_summaries_with_status([source], [event], disclosed)
    assert normalized.summaries == [source]
    carry = _resolve_dividend_carry(normalized.summaries, [event], 80.0, disclosed)
    assert carry.dps_actual_annual == 8.0
    assert carry.dividend_yield == 0.1
    assert carry.basis == "actual_reported"
    assert carry.split_factor is None


def test_rights_and_split_sequence_applies_only_split_to_share_counts():
    source = _summary("1111", date(2026, 1, 1), shares_outstanding=100.0)
    events = [
        JQuantsAdjustmentFactorEvent("1111", date(2026, 2, 1), 0.8, "3"),
        JQuantsAdjustmentFactorEvent("1111", date(2026, 3, 1), 0.5, "1"),
    ]
    result = _normalize_summaries_with_status([source], events, date(2026, 3, 2))
    assert result.summaries[0].shares_outstanding == 200.0


@pytest.mark.parametrize("factor", [0.8, 1.0])
def test_rights_during_holding_need_proceeds_or_exercise_facts_for_total_return(factor):
    event = JQuantsAdjustmentFactorEvent("1111", date(2025, 6, 1), factor, "3")
    result = _resolve_total_return(
        [],
        [],
        adjustment_events=[event],
        price_basis_date=date(2025, 12, 31),
        entry_date=date(2025, 1, 1),
        exit_date=date(2025, 12, 31),
        entry_close=100.0 * factor,
        price_return=0.1,
        adjustment_coverage="complete",
    )
    assert result == (None, 0, None, "unresolved_rights_issue")


def test_rights_after_exit_do_not_change_an_already_completed_total_return():
    exit_date = date(2025, 12, 31)
    event = JQuantsAdjustmentFactorEvent("1111", date(2026, 3, 1), 0.8, "3")
    dividend = _FYDividendObservation(exit_date, date(2026, 2, 1), 8.0)
    result = _resolve_total_return(
        [],
        [dividend],
        adjustment_events=[event],
        price_basis_date=date(2026, 6, 30),
        entry_date=date(2025, 1, 1),
        exit_date=exit_date,
        entry_close=80.0,
        price_return=0.1,
        adjustment_coverage="complete",
    )
    cash_on_price_basis, count, total, status = result
    assert cash_on_price_basis == pytest.approx(6.4)
    assert count == 1
    assert total == pytest.approx(0.18)
    assert status == "resolved"
