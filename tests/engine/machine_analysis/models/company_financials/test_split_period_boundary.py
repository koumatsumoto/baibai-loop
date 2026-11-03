from datetime import date

from baibai_engine.machine_analysis.models.company_financials.metrics.capital import (
    _normalize_summaries_with_status,
)
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
