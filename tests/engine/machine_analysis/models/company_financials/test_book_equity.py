"""低PBRの資本と低収益を、別時点・別帰属の額で説明しないための出典。"""

from dataclasses import replace
from datetime import date

import pytest

from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    build_financial_snapshot,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EdinetMetricRecord
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)

AS_OF = date(2026, 11, 10)


def row(**changes):
    return replace(
        JQuantsFinancialSummary(
            ticker="1001",
            disclosed_at=date(2026, 8, 12),
            disclosed_time="15:30:00",
            disclosure_number="q1",
            document_type="1QFinancialStatements_Consolidated_JP",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            fiscal_year_end=date(2027, 3, 31),
            fiscal_period="1Q",
            shares_outstanding=100.0,
            treasury_shares=20.0,
            total_assets=4000.0,
            equity_to_asset_ratio=0.6,
            bps=30.0,
            equity=3500.0,
        ),
        **changes,
    )


def snapshot(*rows, edinet=None):
    return build_financial_snapshot(
        10.0, rows, edinet, DEFAULT_TTM_RULES, adjustment_events=(), asof_date=AS_OF
    )


def test_book_records_the_selected_denominator_not_net_assets_or_edinet_scope():
    result = snapshot(
        row(), edinet=EdinetMetricRecord(ticker="1001", consolidation_basis="standalone")
    )
    assert result.pbr == pytest.approx(800.0 / 2400.0)
    assert result.book_equity == {
        "amount_yen": 2400.0,
        "method": "assets_times_equity_ratio",
        "measurement_date": date(2026, 6, 30),
        "statement_basis": "consolidated",
        "accounting_standard": "JP",
        "disclosure_number": "q1",
        "disclosed_at": date(2026, 8, 12),
        "disclosed_time": "15:30:00",
        "document_type": "1QFinancialStatements_Consolidated_JP",
        "operands": {"total_assets_yen": 4000.0, "equity_to_asset_ratio": 0.6},
    }
    assert result.consolidation_basis == "standalone"


def test_newer_forecast_does_not_redate_retained_book():
    forecast = JQuantsFinancialSummary(
        ticker="1001",
        disclosed_at=date(2026, 11, 10),
        disclosure_number="guidance",
        document_type="EarnForecastRevision",
        forecast_profit=50.0,
    )
    result = snapshot(row(), forecast)
    assert result.latest_financial_disclosure_date == date(2026, 11, 10)
    assert result.book_equity["disclosure_number"] == "q1"
    assert result.book_equity["measurement_date"] == date(2026, 6, 30)
    assert result.book_equity["disclosed_at"] == date(2026, 8, 12)


@pytest.mark.parametrize("total_assets", [None, 5000.0])
def test_missing_or_disagreeing_yen_route_exposes_bps_and_current_share_basis(total_assets):
    result = snapshot(row(total_assets=total_assets))
    assert result.book_equity["amount_yen"] == 2400.0
    assert result.book_equity["method"] == "bps_times_current_shares"
    assert result.book_equity["operands"] == {
        "bps_yen": 30.0,
        "shares_ex_treasury": 80.0,
        "share_basis_as_of": AS_OF,
    }
    assert result.pbr == pytest.approx(800.0 / 2400.0)


def test_newer_bps_keeps_its_own_source_instead_of_old_same_state_equity():
    newer = row(
        disclosed_at=date(2026, 11, 10),
        disclosure_number="h1",
        period_end=date(2026, 9, 30),
        fiscal_period="2Q",
        bps=35.0,
        total_assets=None,
        equity_to_asset_ratio=None,
    )
    result = snapshot(row(), newer)
    assert result.same_state_equity_yen == 2400.0
    assert result.book_equity["amount_yen"] == 2800.0
    assert result.book_equity["method"] == "bps_times_current_shares"
    assert result.book_equity["disclosure_number"] == "h1"
    assert result.book_equity["measurement_date"] == date(2026, 9, 30)


def test_missing_dates_are_not_inferred_from_disclosure_or_model_date():
    result = snapshot(row(period_start=None, period_end=None, document_type=None))
    assert result.book_equity["measurement_date"] is None
    assert result.book_equity["statement_basis"] == "primary_unknown"
    assert result.book_equity["accounting_standard"] is None


@pytest.mark.parametrize("bps", [None, -30.0, 0.0])
def test_missing_or_nonpositive_book_does_not_become_a_positive_asset_value(bps):
    result = snapshot(row(bps=bps, total_assets=None, equity_to_asset_ratio=None))
    assert result.pbr is None
    if bps is None:
        assert result.book_equity is None
    else:
        assert result.book_equity["amount_yen"] == bps * 80
