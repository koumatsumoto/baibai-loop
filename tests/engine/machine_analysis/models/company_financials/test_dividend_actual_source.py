"""実績配当の報告期を、最新予想年度や支払権利へ読み替えない。"""

from dataclasses import replace
from datetime import date

import pytest

from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    build_financial_snapshot,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import (
    JQuantsAdjustmentFactorEvent,
)

ASOF = date(2026, 11, 14)


def annual(year=2025, dps=40.0):
    return JQuantsFinancialSummary(
        ticker="9999",
        disclosed_at=date(year + 1, 2, 15),
        disclosed_time="15:30:00",
        disclosure_number=f"synthetic-{year}",
        document_type="FYFinancialStatements_Consolidated_JP",
        fiscal_period="FY",
        fiscal_year_end=date(year, 12, 31),
        period_start=date(year, 1, 1),
        period_end=date(year, 12, 31),
        dps_actual_annual=dps,
    )


def current():
    return replace(
        annual(2026, None),
        disclosed_at=date(2026, 11, 13),
        disclosure_number="synthetic-current-q3",
        document_type="3QFinancialStatements_Consolidated_JP",
        fiscal_period="3Q",
        period_end=date(2026, 9, 30),
        forecast_period_start=date(2026, 1, 1),
        forecast_period_end=date(2026, 12, 31),
        forecast_basis="issuer_dividend",
        forecast_target_basis="issuer_dividend",
        dividend_forecast_status="missing",
    )


def snapshot(*rows, events=()):
    return build_financial_snapshot(
        1000, rows, None, DEFAULT_TTM_RULES, adjustment_events=events, asof_date=ASOF
    )


@pytest.mark.parametrize("year", [2024, 2025])
@pytest.mark.parametrize("dps", [0.0, 40.0])
def test_actual_source_keeps_reported_year_when_current_guidance_is_missing(year, dps):
    result = snapshot(annual(year, dps), current())
    assert (result.dps_actual_annual, result.dividend_yield) == (dps, dps / 1000)
    assert result.dividend_basis == "actual_reported"
    assert result.dps_forecast_annual is None
    assert result.dividend_forecast_source["status"] == "missing"
    assert result.dividend_forecast_source["period_end"] == "2026-12-31"
    assert result.dividend_actual_source == {
        "period_type": "FY",
        "period_start": f"{year}-01-01",
        "period_end": f"{year}-12-31",
        "basis": "issuer_dividend",
        "documents": [
            {
                "disclosure_number": f"synthetic-{year}",
                "disclosed_at": f"{year + 1}-02-15",
                "disclosed_time": "15:30:00",
                "document_type": "FYFinancialStatements_Consolidated_JP",
            }
        ],
    }


def test_actual_source_follows_period_first_selection_not_late_old_year_correction():
    corrected_old = replace(annual(2024, 20.0), disclosed_at=date(2026, 11, 14))
    result = snapshot(corrected_old, annual(), current())
    assert result.dps_actual_annual == 40.0
    assert result.dividend_actual_source["period_end"] == "2025-12-31"
    assert result.dividend_actual_source["documents"][0]["disclosure_number"] == "synthetic-2025"


@pytest.mark.parametrize("conflict", [False, True])
def test_same_day_sources_remain_order_independent_without_inventing_unknown_time(conflict):
    known = annual()
    unknown = replace(
        known,
        disclosed_time=None,
        disclosure_number="unknown-time",
        dps_actual_annual=0.0 if conflict else 40.0,
    )
    first, second = snapshot(known, unknown, current()), snapshot(current(), unknown, known)
    assert first == second
    assert first.dps_actual_annual == (None if conflict else 40.0)
    assert first.dividend_basis == ("unresolved_actual_dividend" if conflict else "actual_reported")
    assert [item["disclosed_time"] for item in first.dividend_actual_source["documents"]] == [
        None,
        "15:30:00",
    ]


@pytest.mark.parametrize("dps", [0.0, 40.0])
def test_missing_period_remains_unknown_in_actual_source(dps):
    result = snapshot(replace(annual(dps=dps), period_start=None), current())
    assert result.dividend_actual_source["period_start"] is None
    assert result.dividend_actual_source["period_end"] == "2025-12-31"
    assert result.dps_actual_annual == (0.0 if dps == 0 else None)


@pytest.mark.parametrize("dps", [0.0, 40.0])
def test_unresolved_split_basis_does_not_erase_actual_period_or_turn_positive_cash_into_zero(dps):
    event = JQuantsAdjustmentFactorEvent(
        ticker="9999", traded_at=date(2026, 6, 1), adjustment_factor=0.5
    )
    result = snapshot(annual(dps=dps), current(), events=(event,))
    assert result.dividend_actual_source["period_end"] == "2025-12-31"
    assert result.dps_actual_annual == (0.0 if dps == 0 else None)
    assert result.dividend_yield == (0.0 if dps == 0 else None)


def test_forecast_basis_and_missing_actual_do_not_invent_a_reported_source():
    forecast = replace(current(), dividend_forecast_status="confirmed", dps_forecast_annual=50.0)
    missing, present = snapshot(forecast), snapshot(annual(), forecast)
    assert missing.dividend_actual_source is None
    assert present.dividend_actual_source["period_end"] == "2025-12-31"
    for result in (missing, present):
        assert result.dividend_basis == "forecast_annual"
        assert result.dps_forecast_annual == 50.0
        assert result.dividend_yield == 0.05


def test_post_asof_actual_source_does_not_replace_available_observation():
    future = replace(annual(2026, 100.0), disclosed_at=date(2027, 2, 15))
    result = snapshot(annual(), current(), future)
    assert result.dps_actual_annual == 40.0
    assert result.dividend_actual_source["period_end"] == "2025-12-31"
