"""歴史評価の年間flowと期末株数を、実際のFY区間で比較する。"""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.models.company_financials.metrics.dividends import (
    build_shareholder_return_change_signals,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.profit import (
    build_normalized_profit_signals,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)

ASOF = date(2026, 6, 1)


def fy_rows(calendar="month_end"):
    if calendar == "month_end":
        periods = [
            (date(year - 1, 3, 1), date(year, 2, 29 if year == 2024 else 28))
            for year in range(2021, 2027)
        ]
    else:
        start = date(2020, 2, 23) if calendar == "52_week" else date(2020, 2, 16)
        periods = []
        for days in [364] * 3 + ([371] if calendar == "53_week" else [364]) + [364] * 2:
            end = start + timedelta(days=days - 1)
            periods.append((start, end))
            start = end + timedelta(days=1)
    return [
        JQuantsFinancialSummary(
            ticker="1001",
            disclosed_at=end + timedelta(days=45),
            fiscal_period="FY",
            fiscal_year_end=end,
            period_start=start,
            period_end=end,
            cumulative_eps=10.0,
            dps_actual_annual=float(index + 1),
            shares_outstanding=100.0 - index,
            document_type="FYFinancialStatements_Consolidated_JP",
        )
        for index, (start, end) in enumerate(periods)
    ]


@pytest.mark.parametrize("calendar", ["month_end", "52_week", "53_week"])
def test_adjoining_full_years_support_profit_dividend_and_capital_series(calendar):
    rows = fy_rows(calendar)
    profit = build_normalized_profit_signals(
        rows, (), ASOF, close=100.0, ttm_rules=DEFAULT_TTM_RULES
    )
    returns = build_shareholder_return_change_signals(rows, (), ASOF)
    assert profit.normalized_per_3fy == 10.0
    assert profit.normalized_per_5fy == 10.0
    assert returns.dps_streak_up is True
    assert returns.dps_yoy_latest == pytest.approx(0.2)
    assert returns.share_count_reduction_streak == 2


@pytest.mark.parametrize(
    "problem",
    [
        "eleven_month",
        "gap",
        "overlap",
        "missing_period",
        "missing_value",
        "conflicting_value",
        "conflicting_period",
    ],
)
def test_latest_ineligible_fy_is_not_filled_with_an_older_year(problem):
    rows = fy_rows()
    latest = rows[-1]
    if problem == "eleven_month":
        # 隣接する短年度。株数は期末の事実だが、EPS/DPSは年間額ではない。
        rows[-1] = replace(latest, period_end=date(2026, 1, 31), fiscal_year_end=date(2026, 1, 31))
    elif problem in {"gap", "overlap"}:
        rows[-1] = replace(
            latest, period_start=date(2025, 3, 2) if problem == "gap" else date(2025, 2, 28)
        )
    elif problem == "missing_period":
        rows[-1] = replace(latest, period_start=None)
    elif problem == "missing_value":
        rows[-1] = replace(
            latest, cumulative_eps=None, dps_actual_annual=None, shares_outstanding=None
        )
    elif problem == "conflicting_value":
        rows.append(
            replace(latest, cumulative_eps=20.0, dps_actual_annual=12.0, shares_outstanding=90.0)
        )
    else:
        # 同じFY label・同じ無配でも、矛盾する期間から連続性を選ばない。
        rows = [replace(row, dps_actual_annual=0.0) for row in rows]
        rows.append(replace(rows[-1], period_start=date(2025, 3, 2)))
    profit = build_normalized_profit_signals(
        rows, (), ASOF, close=100.0, ttm_rules=DEFAULT_TTM_RULES
    )
    returns = build_shareholder_return_change_signals(rows, (), ASOF)
    assert profit.normalized_per_3fy is None
    assert profit.normalized_per_5fy is None
    assert returns.dps_streak_up is None
    assert returns.dps_yoy_latest is None
    assert returns.share_count_reduction_streak == (2 if problem == "eleven_month" else None)
