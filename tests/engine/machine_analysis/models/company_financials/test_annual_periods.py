"""財務・成長の両ownerで、週次決算暦と実期間の連続性を揃える。"""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.periods import (
    _prior_year_summary,
    _ttm_period,
    _ttm_value,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.profit import (
    build_normalized_profit_signals,
    build_profitability_level_signals,
)
from baibai_engine.machine_analysis.models.company_growth.calculations import build_period_states
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.financials.metric_quality import TTMQuality


def document(start, end, amount, *, fiscal_period="FY", fiscal_year_end=None):
    return JQuantsFinancialSummary(
        ticker="1001",
        disclosed_at=end + timedelta(days=45),
        fiscal_period=fiscal_period,
        fiscal_year_end=fiscal_year_end or end,
        period_start=start,
        period_end=end,
        sales=amount * 10,
        operating_profit=amount,
        profit=amount,
        disclosure_number=f"week-calendar-{end}",
        document_type="FinancialStatements_Consolidated_JP",
    )


def weekly_documents(prior_start):
    prior_end = date(2024, 2, 17)
    return [
        document(
            prior_start,
            date(2023, 5, 20),
            20.0,
            fiscal_period="1Q",
            fiscal_year_end=prior_end,
        ),
        document(prior_start, prior_end, 100.0),
        document(
            date(2024, 2, 18),
            date(2024, 5, 18),
            40.0,
            fiscal_period="1Q",
            fiscal_year_end=date(2025, 2, 15),
        ),
    ]


def growth_annual(rows):
    state = build_period_states(rows, sector="情報・通信業")[-1]
    return next(item for item in state.observations if item.measurement == "annual_ttm")


@pytest.mark.parametrize("prior_start", [date(2023, 2, 19), date(2023, 2, 12)])
def test_week_calendar_ttm_uses_adjoining_52_or_53_week_fy(prior_start):
    rows = weekly_documents(prior_start)
    assert _ttm_value(rows, "profit", DEFAULT_TTM_RULES) == (120.0, TTMQuality.EXACT)
    period = _ttm_period(rows, "profit", DEFAULT_TTM_RULES)
    assert period is not None
    assert period["start"] == "2023-05-21"
    assert period["end"] == "2024-05-18"
    assert [item["amount_yen"] for item in period["components"]] == [40.0, 100.0, 20.0]
    annual = growth_annual(rows)
    assert annual.period_status == "available"
    assert (annual.period_start, annual.period_end) == (date(2023, 5, 21), date(2024, 5, 18))
    assert annual.metrics["parent_profit"].value == 120.0
    assert len(annual.metrics["parent_profit"].documents) == 3


@pytest.mark.parametrize(
    ("start", "expected"),
    [(date(2024, 2, 25), 100.0), (date(2024, 2, 18), 100.0), (date(2024, 3, 23), None)],
)
def test_reported_52_and_53_week_years_are_annual_but_eleven_months_are_not(start, expected):
    rows = [document(start, date(2025, 2, 22), 100.0)]
    value, quality = _ttm_value(rows, "profit", DEFAULT_TTM_RULES)
    assert value == expected
    assert quality == (TTMQuality.EXACT if expected is not None else TTMQuality.UNAVAILABLE)
    annual = growth_annual(rows)
    assert annual.metrics["parent_profit"].value == expected
    if expected is not None:
        assert annual.period_status == "available"
        assert annual.period_start == start


@pytest.mark.parametrize("incomparable", ["gap", "basis"])
def test_week_calendar_support_does_not_hide_a_gap_or_mix_statement_bases(incomparable):
    rows = weekly_documents(date(2023, 2, 19))
    if incomparable == "gap":
        # Keep the annual metadata but remove one day from the reported FY interval.
        rows[1] = replace(rows[1], period_end=date(2024, 2, 16))
    else:
        rows[1] = replace(rows[1], document_type="FinancialStatements_NonConsolidated_JP")
    assert _ttm_value(rows, "profit", DEFAULT_TTM_RULES) == (None, TTMQuality.UNAVAILABLE)
    assert growth_annual(rows).metrics["parent_profit"].value is None


def test_jp_to_ifrs_transition_keeps_reported_amount_but_blocks_ttm_and_yoy():
    rows = weekly_documents(date(2023, 2, 19))
    rows[2] = replace(rows[2], document_type="1QFinancialStatements_Consolidated_IFRS")
    assert rows[2].profit == 40.0
    assert _ttm_value(rows, "profit", DEFAULT_TTM_RULES) == (None, TTMQuality.UNAVAILABLE)
    assert _prior_year_summary(rows, DEFAULT_TTM_RULES, field_name="profit") is None
    assert growth_annual(rows).metrics["parent_profit"].reason == (
        "accounting_standard_incomparable"
    )


def test_same_time_standard_conflict_cannot_pick_an_arbitrary_ttm_component():
    rows = weekly_documents(date(2023, 2, 19))
    rows.append(
        replace(
            rows[2],
            document_type="1QFinancialStatements_Consolidated_IFRS",
            disclosure_number="same-time-ifrs",
        )
    )
    assert _ttm_value(rows, "profit", DEFAULT_TTM_RULES) == (None, TTMQuality.UNAVAILABLE)


def test_share_count_yoy_is_exempt_from_accounting_standard_comparison():
    rows = weekly_documents(date(2023, 2, 19))
    rows[0] = replace(rows[0], shares_outstanding=100.0)
    rows[2] = replace(
        rows[2], shares_outstanding=120.0,
        document_type="1QFinancialStatements_Consolidated_IFRS",
    )
    prior = _prior_year_summary(rows, DEFAULT_TTM_RULES, field_name="shares_outstanding")
    assert prior is not None and prior.shares_outstanding == 100.0


def test_later_partial_correction_cannot_restore_an_old_basis_profit():
    rows = weekly_documents(date(2023, 2, 19))
    rows.append(
        replace(
            rows[0], disclosed_at=date(2023, 7, 15), disclosure_number="ifrs-correction",
            document_type="1QFinancialStatements_Consolidated_IFRS", profit=None,
        )
    )
    assert _prior_year_summary(rows, DEFAULT_TTM_RULES, field_name="profit") is None
    assert _ttm_value(rows, "profit", DEFAULT_TTM_RULES) == (None, TTMQuality.UNAVAILABLE)


def test_multi_fy_average_cannot_mix_jp_and_ifrs_eps():
    rows = [
        replace(
            document(date(year, 4, 1), date(year + 1, 3, 31), 100.0),
            cumulative_eps=10.0,
        )
        for year in (2022, 2023, 2024)
    ]
    def signals(items):
        return build_normalized_profit_signals(
            items, (), date(2025, 6, 30), close=100.0, ttm_rules=DEFAULT_TTM_RULES
        )

    assert signals(rows).normalized_per_3fy == 10.0
    tied = replace(
        rows[-1], disclosure_number="same-time-ifrs",
        document_type="FYFinancialStatements_Consolidated_IFRS",
    )
    assert signals([*rows, tied]).normalized_per_3fy is None
    rows[-1] = replace(rows[-1], document_type="FYFinancialStatements_Consolidated_IFRS")
    assert signals(rows).normalized_per_3fy is None


def test_profitability_ratio_does_not_use_old_jp_assets_with_ifrs_flows():
    rows = [
        replace(document(date(2023, 4, 1), date(2024, 3, 31), 100.0), total_assets=1000.0),
        replace(
            document(date(2024, 4, 1), date(2024, 6, 30), 20.0, fiscal_period="1Q",
                     fiscal_year_end=date(2025, 3, 31)),
            document_type="1QFinancialStatements_Consolidated_IFRS",
        ),
        replace(
            document(date(2024, 4, 1), date(2025, 3, 31), 100.0),
            document_type="FYFinancialStatements_Consolidated_IFRS",
        ),
        replace(
            document(date(2025, 4, 1), date(2025, 6, 30), 30.0, fiscal_period="1Q",
                     fiscal_year_end=date(2026, 3, 31)),
            document_type="1QFinancialStatements_Consolidated_IFRS",
        ),
    ]
    result = build_profitability_level_signals(rows, date(2025, 8, 31), DEFAULT_TTM_RULES)
    assert result.operating_margin == 0.1
    assert result.operating_profit_to_assets is None
    assert result.asset_turnover is None
