from datetime import date

import pytest

from baibai_engine.machine_analysis.models.company_financials.metrics.capital import (
    _normalize_summaries_with_status,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.periods import _carry_forward
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
)


def row(**kwargs):
    return JQuantsFinancialSummary(ticker="7988", **kwargs)


def test_split_effective_after_period_preserves_already_adjusted_bps_and_eps():
    before = row(
        disclosed_at=date(2026, 7, 31),
        period_end=date(2026, 6, 30),
        shares_outstanding=100232614.0,
        treasury_shares=7106925.0,
        average_shares=93125689.0,
        total_assets=390111000000.0,
        equity_to_asset_ratio=0.766,
        bps=1603.7,
        profit=8890000000.0,
        cumulative_eps=47.73,
    )
    latest = row(
        disclosed_at=date(2026, 10, 30),
        period_end=date(2026, 9, 30),
        shares_outstanding=100232614.0,
        treasury_shares=14602669.0,
        average_shares=92069656.0,
        total_assets=377996000000.0,
        equity_to_asset_ratio=0.715,
        bps=1577.09,
        profit=17214000000.0,
        cumulative_eps=93.48,
    )
    split = JQuantsAdjustmentFactorEvent("7988", date(2026, 9, 29), 0.5, "1")
    result = _normalize_summaries_with_status([before, latest], [split], date(2026, 11, 3))
    earlier, current = result.summaries
    assert earlier.bps == 1603.7
    assert earlier.cumulative_eps == 47.73
    assert current.shares_outstanding == 200465228
    assert current.treasury_shares == 29205338
    assert current.bps == 1577.09
    assert current.cumulative_eps == 93.48
    assert {item["method"] for item in result.basis_adjustments} == {
        "share_count_and_per_share_basis_crosschecked",
        "already_adjusted_per_share_crosschecked",
    }


def test_no_correction_without_an_independent_equity_crosscheck():
    before = row(
        disclosed_at=date(2026, 7, 31), period_end=date(2026, 6, 30), shares_outstanding=100.0
    )
    latest = row(
        disclosed_at=date(2026, 10, 30),
        period_end=date(2026, 9, 30),
        shares_outstanding=100.0,
        treasury_shares=10.0,
        bps=5.0,
    )
    split = JQuantsAdjustmentFactorEvent("7988", date(2026, 9, 29), 0.5, "1")
    result = _normalize_summaries_with_status([before, latest], [split], date(2026, 11, 3))
    assert result.summaries[-1].shares_outstanding == 100


def test_no_correction_when_eps_does_not_confirm_the_bps_hypothesis():
    before = row(
        disclosed_at=date(2026, 7, 31),
        period_end=date(2026, 6, 30),
        shares_outstanding=100.0,
    )
    latest = row(
        disclosed_at=date(2026, 10, 30),
        period_end=date(2026, 9, 30),
        shares_outstanding=100.0,
        treasury_shares=10.0,
        bps=5.0,
        total_assets=900.0,
        equity_to_asset_ratio=1.0,
        average_shares=90.0,
        profit=900.0,
        cumulative_eps=10.0,
    )
    split = JQuantsAdjustmentFactorEvent("7988", date(2026, 9, 29), 0.5, "1")
    result = _normalize_summaries_with_status([before, latest], [split], date(2026, 11, 3))
    assert result.summaries[-1].shares_outstanding == 100


def test_fiscal_year_crossing_ex_date_keeps_retroactively_adjusted_eps():
    before = row(
        disclosed_at=date(2026, 2, 12),
        period_end=date(2025, 12, 31),
        shares_outstanding=12150000.0,
        treasury_shares=280228.0,
    )
    latest = row(
        disclosed_at=date(2026, 5, 14),
        period_end=date(2026, 3, 31),
        shares_outstanding=12000000.0,
        treasury_shares=159471.0,
        average_shares=12614586.0,
        bps=1815.81,
        total_assets=71444000000.0,
        equity_to_asset_ratio=0.602,
        profit=8071000000.0,
        cumulative_eps=319.92,
    )
    split = JQuantsAdjustmentFactorEvent("7988", date(2026, 3, 30), 0.5, "1")
    result = _normalize_summaries_with_status([before, latest], [split], date(2026, 11, 3))
    assert result.summaries[-1].shares_outstanding == 24000000
    assert result.summaries[-1].bps == 1815.81
    assert result.summaries[-1].cumulative_eps == 319.92


def test_future_split_does_not_retroactively_change_an_earlier_asof():
    before = row(
        disclosed_at=date(2026, 7, 31), period_end=date(2026, 6, 30), shares_outstanding=100.0
    )
    latest = row(
        disclosed_at=date(2026, 8, 15),
        period_end=date(2026, 9, 30),
        shares_outstanding=100.0,
        treasury_shares=10.0,
        average_shares=90.0,
        bps=5.0,
        total_assets=900.0,
        equity_to_asset_ratio=1.0,
        profit=900.0,
        cumulative_eps=5.0,
    )
    split = JQuantsAdjustmentFactorEvent("7988", date(2026, 9, 29), 0.5, "1")
    result = _normalize_summaries_with_status([before, latest], [split], date(2026, 8, 31))
    assert result.summaries[-1].shares_outstanding == 100


def test_two_separate_splits_do_not_double_adjust_per_share_values():
    before = row(
        disclosed_at=date(2026, 1, 31), period_end=date(2025, 12, 31), shares_outstanding=100.0
    )
    latest = row(
        disclosed_at=date(2026, 5, 1),
        period_end=date(2026, 3, 31),
        shares_outstanding=100.0,
        treasury_shares=10.0,
        average_shares=90.0,
        bps=5.0,
        total_assets=900.0,
        equity_to_asset_ratio=1.0,
        profit=900.0,
        cumulative_eps=5.0,
    )
    first = JQuantsAdjustmentFactorEvent("7988", date(2026, 3, 30), 0.5, "1")
    second = JQuantsAdjustmentFactorEvent("7988", date(2026, 6, 1), 0.5, "1")
    result = _normalize_summaries_with_status([before, latest], [first, second], date(2026, 7, 1))
    current = result.summaries[-1]
    assert current.shares_outstanding == 400
    assert current.treasury_shares == 40
    assert current.bps == 2.5
    assert current.cumulative_eps == 2.5


@pytest.mark.parametrize(
    ("first_factor", "second_factor"),
    [(0.5, 0.5), (0.5, 2.0), (2.0, 0.5), (2.0, 2.0)],
)
@pytest.mark.parametrize("profit", [-900.0, 900.0])
def test_only_first_announced_split_is_already_reflected(first_factor, second_factor, profit):
    source = row(
        disclosed_at=date(2026, 7, 31),
        period_end=date(2026, 6, 30),
        shares_outstanding=100.0,
        treasury_shares=10.0,
        average_shares=90.0,
        bps=10.0 * first_factor,
        total_assets=900.0,
        equity_to_asset_ratio=1.0,
        profit=profit,
        cumulative_eps=profit / 90 * first_factor,
        forecast_profit=profit * 2,
        forecast_eps=profit / 90 * 2 * first_factor,
        dps_actual_annual=6.0,
    )
    events = [
        JQuantsAdjustmentFactorEvent(
            "7988", date(2026, 9, 29), first_factor, "1" if first_factor < 1 else "2"
        ),
        JQuantsAdjustmentFactorEvent(
            "7988", date(2026, 12, 1), second_factor, "1" if second_factor < 1 else "2"
        ),
    ]
    result = _normalize_summaries_with_status([source], events, date(2026, 12, 2))
    normalized = result.summaries[0]
    factor = first_factor * second_factor
    assert normalized.shares_outstanding == 100 / factor
    assert normalized.treasury_shares == 10 / factor
    assert normalized.average_shares == 90 / factor
    assert normalized.bps == 10 * factor
    assert normalized.cumulative_eps == profit / 90 * factor
    assert normalized.forecast_eps == profit / 90 * 2 * factor
    assert normalized.dps_actual_annual == 6
    assert result.basis_adjustments[0]["bps_conversion_factor"] == second_factor


def test_each_per_share_field_needs_its_own_reflected_basis():
    source = row(
        disclosed_at=date(2026, 7, 31),
        period_end=date(2026, 6, 30),
        shares_outstanding=100.0,
        treasury_shares=10.0,
        average_shares=90.0,
        bps=5.0,
        total_assets=900.0,
        equity_to_asset_ratio=1.0,
        profit=900.0,
        cumulative_eps=10.0,
        forecast_profit=1800.0,
        forecast_eps=5.0,
    )
    events = [
        JQuantsAdjustmentFactorEvent("7988", date(2026, 9, 29), 0.5, "1"),
        JQuantsAdjustmentFactorEvent("7988", date(2026, 12, 1), 0.5, "1"),
    ]
    result = _normalize_summaries_with_status([source], events, date(2026, 12, 2))
    normalized = result.summaries[0]
    assert normalized.bps == 2.5
    assert normalized.cumulative_eps == 2.5
    assert normalized.forecast_eps == 5.0
    adjustment = result.basis_adjustments[0]
    assert adjustment["bps_conversion_factor"] == 0.5
    assert adjustment["eps_conversion_factor"] == 0.25
    assert adjustment["forecast_eps_conversion_factor"] == 1.0


def test_ambiguous_intermediate_basis_does_not_guess_a_per_share_value():
    source = row(
        disclosed_at=date(2026, 7, 31),
        period_end=date(2026, 6, 30),
        shares_outstanding=100.0,
        treasury_shares=10.0,
        average_shares=90.0,
        bps=5.05,
        total_assets=900.0,
        equity_to_asset_ratio=1.0,
        profit=900.0,
        cumulative_eps=10.0,
    )
    events = [
        JQuantsAdjustmentFactorEvent("7988", date(2026, 9, 29), 0.5, "1"),
        JQuantsAdjustmentFactorEvent("7988", date(2026, 12, 1), 1.02, "2"),
    ]
    prior = row(
        disclosed_at=date(2026, 5, 1),
        period_end=date(2026, 3, 31),
        shares_outstanding=100.0,
        treasury_shares=10.0,
        bps=10.0,
    )
    result = _normalize_summaries_with_status([prior, source], events, date(2026, 12, 2))
    normalized = result.summaries[-1]
    assert normalized.bps is None
    assert normalized.cumulative_eps == 5.1
    assert normalized.shares_outstanding == 100 / 0.51
    assert normalized.total_assets == 900
    assert normalized.unresolved_actual_fields == ("bps",)
    assert _carry_forward(result.summaries, "bps", normalized) == (None, None)
    assert result.basis_adjustments[0]["bps_conversion_factor"] is None


def test_intermediate_basis_without_its_independent_amount_is_not_asserted():
    source = row(
        disclosed_at=date(2026, 7, 31),
        period_end=date(2026, 6, 30),
        shares_outstanding=100.0,
        treasury_shares=10.0,
        bps=5.0,
        # A matching equity route is not evidence about forecast EPS.
        total_assets=900.0,
        equity_to_asset_ratio=1.0,
        forecast_eps=10.0,
    )
    events = [
        JQuantsAdjustmentFactorEvent("7988", date(2026, 9, 29), 0.5, "1"),
        JQuantsAdjustmentFactorEvent("7988", date(2026, 12, 1), 0.5, "1"),
    ]
    normalized = _normalize_summaries_with_status([source], events, date(2026, 12, 2)).summaries[0]
    assert normalized.bps == 2.5
    assert normalized.forecast_eps is None
