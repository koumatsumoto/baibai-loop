"""低PBRの資本と低収益を、別時点・別帰属の額で説明しないための出典。"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    build_financial_snapshot,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.observations.financials.capital_action_pdf import CapitalAction
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


def snapshot(*rows, edinet=None, capital_actions=(), adjustment_events=()):
    return build_financial_snapshot(
        10.0,
        rows,
        edinet,
        DEFAULT_TTM_RULES,
        adjustment_events=adjustment_events,
        asof_date=AS_OF,
        capital_actions=capital_actions,
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


COMPLETED_ISSUANCE = CapitalAction(
    ticker="1001",
    issued_shares=150,
    effective_date=date(2026, 9, 1),
    published_on=date(2026, 9, 1),
    received_at=datetime(2026, 9, 2, tzinfo=UTC),
    source_url="https://example.invalid/completed.pdf",
    source_sha256="a" * 64,
    disclosure_number="completed-issuance",
    description="completed",
)


def later_capital(**changes):
    return replace(
        row(
            disclosed_at=AS_OF,
            disclosure_number="h1-capital",
            period_end=date(2026, 9, 30),
            fiscal_period="2Q",
            shares_outstanding=150.0,
            treasury_shares=20.0,
            bps=None,
            total_assets=None,
            equity_to_asset_ratio=None,
        ),
        **changes,
    )


@pytest.mark.parametrize("disclosed_at", [date(2026, 8, 12), date(2026, 10, 10)])
def test_old_bps_cannot_create_new_book_after_completed_issuance(disclosed_at):
    old = row(total_assets=None, equity_to_asset_ratio=None, disclosed_at=disclosed_at)
    current = later_capital()
    result = snapshot(old, current, capital_actions=[COMPLETED_ISSUANCE])
    assert result.shares_ex_treasury == 130
    assert result.market_cap == 1300
    assert result.capital_basis_failure_reason is None
    assert result.book_equity is None
    assert result.pbr is None
    assert old.bps == 30
    assert old.period_end == date(2026, 6, 30)
    assert current.bps is None


@pytest.mark.parametrize("period_end", [date(2026, 9, 1), date(2026, 9, 30)])
def test_new_bps_on_or_after_completed_issuance_restores_book(period_end):
    result = snapshot(
        row(total_assets=None, equity_to_asset_ratio=None),
        later_capital(bps=40.0, period_end=period_end),
        capital_actions=[COMPLETED_ISSUANCE],
    )
    assert result.market_cap == 1300
    assert result.book_equity["amount_yen"] == 5200
    assert result.book_equity["measurement_date"] == period_end
    assert result.book_equity["disclosure_number"] == "h1-capital"
    assert result.pbr == 0.25


@pytest.mark.parametrize("new_yen_book", [False, True])
def test_independently_selected_yen_book_is_not_disabled(new_yen_book):
    current = later_capital(
        total_assets=6000.0 if new_yen_book else None,
        equity_to_asset_ratio=0.6 if new_yen_book else None,
    )
    result = snapshot(row(), current, capital_actions=[COMPLETED_ISSUANCE])
    assert result.market_cap == 1300
    assert result.book_equity["method"] == "assets_times_equity_ratio"
    assert result.book_equity["amount_yen"] == (3600 if new_yen_book else 2400)
    assert result.book_equity["measurement_date"] == (
        date(2026, 9, 30) if new_yen_book else date(2026, 6, 30)
    )


def test_unusable_bps_does_not_revive_a_disagreeing_yen_route():
    result = snapshot(
        row(total_assets=5000.0), later_capital(), capital_actions=[COMPLETED_ISSUANCE]
    )
    assert result.market_cap == 1300
    assert result.same_state_equity_yen == 3000
    assert result.book_equity is None
    assert result.pbr is None


def test_no_completed_event_keeps_existing_bps_policy():
    result = snapshot(row(total_assets=None, equity_to_asset_ratio=None), later_capital())
    assert result.book_equity["amount_yen"] == 3900
    assert result.book_equity["method"] == "bps_times_current_shares"
    assert result.pbr == pytest.approx(1300 / 3900)


def test_valid_split_adjustment_preserves_book_amount():
    from baibai_engine.machine_analysis.observations.prices.price_models import (
        JQuantsAdjustmentFactorEvent,
    )

    result = snapshot(
        row(total_assets=None, equity_to_asset_ratio=None),
        adjustment_events=[JQuantsAdjustmentFactorEvent("1001", date(2026, 9, 1), 0.5, "1")],
    )
    assert result.shares_ex_treasury == 160
    assert result.book_equity["amount_yen"] == 2400
    assert result.book_equity["operands"]["bps_yen"] == 15
    assert result.pbr == pytest.approx(1600 / 2400)


def test_generation_keeps_the_event_and_current_price_without_synthetic_book():
    from baibai_engine.machine_analysis.generation.financial_models import (
        generate_financial_and_price_models,
    )
    from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
    from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar

    models = generate_financial_and_price_models(
        ticker="1001",
        documents=[row(total_assets=None, equity_to_asset_ratio=None), later_capital()],
        adjusted_bars=[PriceBar(AS_OF, 10.0, None)],
        adjustment_events=(),
        trading_dates=tuple(AS_OF - timedelta(days=252 - index) for index in range(253)),
        expected_price_date=AS_OF,
        sector=None,
        as_of=AS_OF,
        input_release=InputRelease(release_id="synthetic", manifest_sha256="b" * 64),
        ttm_rules=DEFAULT_TTM_RULES,
        capital_actions=[COMPLETED_ISSUANCE],
    )
    payload = next(model.payload for model in models if model.model_id == "company_financials")
    assert payload["financial"]["market_cap"] == 1300
    assert payload["financial"]["book_equity"] is None
    assert payload["financial"]["pbr"] is None
    assert (
        payload["capital_action_evidence"][0]["source_sha256"] == COMPLETED_ISSUANCE.source_sha256
    )
