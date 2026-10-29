"""財務・成長の両ownerで、週次決算暦と実期間の連続性を揃える。"""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.periods import (
    _ttm_period,
    _ttm_value,
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
