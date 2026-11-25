"""低PBRの資本と低収益を、別時点・別帰属の額で説明しないための出典。"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    GenerationMetadata,
    InputSnapshot,
)
from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    build_financial_snapshot,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    DEFAULT_TTM_RULES,
)
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials.capital_action_pdf import CapitalAction
from baibai_engine.machine_analysis.observations.financials.edinet_models import EdinetMetricRecord
from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    normalize_financial_summary,
)
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.screening.policies.discovery import project_discovery_row

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
def test_missing_or_disagreeing_yen_route_exposes_bps_and_same_statement_shares(total_assets):
    result = snapshot(row(total_assets=total_assets))
    assert result.book_equity["amount_yen"] == 2400.0
    assert result.book_equity["method"] == "bps_times_reported_period_shares"
    assert result.book_equity["operands"] == {
        "bps_yen": 30.0,
        "shares_ex_treasury": 80.0,
        "share_measurement_date": date(2026, 6, 30),
        "share_unit_basis_as_of": AS_OF,
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
    assert result.book_equity["method"] == "bps_times_reported_period_shares"
    assert result.book_equity["disclosure_number"] == "h1"
    assert result.book_equity["measurement_date"] == date(2026, 9, 30)


def test_missing_dates_are_not_inferred_from_disclosure_or_model_date():
    result = snapshot(row(period_start=None, period_end=None, document_type=None, bps=None))
    assert result.book_equity["measurement_date"] is None
    assert result.book_equity["statement_basis"] == "primary_unknown"
    assert result.book_equity["accounting_standard"] is None


def test_undated_bps_cannot_supply_a_dated_book():
    result = snapshot(row(period_start=None, period_end=None))
    assert result.market_cap == 800
    assert result.book_equity is None
    assert result.pbr is None


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


def test_same_effective_day_issued_mismatch_prevents_bps_book_despite_later_valid_cap():
    old = row(
        total_assets=None,
        equity_to_asset_ratio=None,
        period_end=COMPLETED_ISSUANCE.effective_date,
        disclosed_at=date(2026, 10, 10),
    )
    result = snapshot(old, later_capital(), capital_actions=[COMPLETED_ISSUANCE])
    assert result.market_cap == 1300
    assert result.book_equity is None
    assert result.pbr is None


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


def test_current_shares_do_not_remeasure_book_without_a_completed_event_record():
    result = snapshot(row(total_assets=None, equity_to_asset_ratio=None), later_capital())
    assert result.market_cap == 1300
    assert result.book_equity["amount_yen"] == 2400
    assert result.book_equity["method"] == "bps_times_reported_period_shares"
    assert result.pbr == pytest.approx(1300 / 2400)


@pytest.mark.parametrize("invalid", [{"treasury_shares": None}, {"treasury_shares": 120.0}])
def test_current_shares_cannot_supply_a_missing_or_invalid_book_share_pair(invalid):
    old = row(total_assets=None, equity_to_asset_ratio=None, **invalid)
    result = snapshot(old, later_capital())
    assert result.market_cap == 1300
    assert result.book_equity is None
    assert result.pbr is None


def test_missing_book_shares_do_not_revive_a_disagreeing_ratio():
    earlier = row(total_assets=5000.0)
    current = later_capital(
        bps=50.0, treasury_shares=None, total_assets=6000.0, equity_to_asset_ratio=0.6
    )
    result = snapshot(earlier, current)
    assert result.same_state_equity_yen == 3600
    assert result.book_equity is None
    assert result.pbr is None


def test_same_day_capital_correction_is_not_grafted_onto_a_bps_document():
    old = row(total_assets=None, equity_to_asset_ratio=None)
    correction = replace(
        old,
        disclosure_number="q1-correction",
        disclosed_time="18:00:00",
        bps=None,
        shares_outstanding=150.0,
    )
    result = snapshot(old, correction)
    assert result.market_cap == 1300
    assert result.book_equity is None
    assert result.pbr is None


def test_issuer_bps_given_on_same_day_controls_over_inconsistent_parent_ratio():
    # JAL TDnet 20261102562314 expressly excludes non-common capital from its BPS.
    reported = row(
        disclosed_at=date(2026, 11, 2),
        disclosure_number="20261102562314",
        document_type="2QFinancialStatements_Consolidated_IFRS",
        fiscal_period="2Q",
        period_end=date(2026, 9, 30),
        shares_outstanding=437_143_500,
        treasury_shares=7_191_588,
        bps=2609.12,
        total_assets=3_317_613_000_000,
        equity_to_asset_ratio=0.451,
        equity=1_543_196_000_000,
    )
    result = snapshot(reported)
    assert result.book_equity["method"] == "bps_times_reported_period_shares"
    assert result.book_equity["amount_yen"] == pytest.approx(1_121_796_132_637.44)
    assert result.same_state_equity_yen == 1_496_243_463_000.0
    assert result.pbr == pytest.approx(10.0 / 2609.12)


def test_dated_book_reaches_candidate_with_current_cap_and_its_original_measurement():
    # Reconstruction of reviewed TDnet 20260511521711 / 20261109568673 / 20251105587868
    # and the saved 9434 route. These are test inputs, not archived provider bytes.
    # The 9/30 ordinary BPS (58.87) is in issuer supplemental data, not the TDnet summary.
    trading_day = date(2026, 11, 20)
    evaluation_day = date(2026, 11, 23)
    rows = tuple(
        normalize_financial_summary(
            {
                "Code": "94340",
                "DiscTime": "15:30:00",
                **record,
            }
        )
        for record in (
            {
                "DiscNo": "20251105587868",
                "DiscDate": "2025-11-05",
                "DocType": "2QFinancialStatements_Consolidated_IFRS",
                "CurPerType": "2Q",
                "CurFYEn": "2026-03-31",
                "CurPerSt": "2025-04-01",
                "CurPerEn": "2025-09-30",
                "NP": "348755000000",
            },
            {
                "DiscNo": "20260511521711",
                "DiscDate": "2026-05-11",
                "DocType": "FYFinancialStatements_Consolidated_IFRS",
                "CurPerType": "FY",
                "CurFYEn": "2026-03-31",
                "CurPerSt": "2025-04-01",
                "CurPerEn": "2026-03-31",
                "NP": "550759000000",
                "TA": "18502175000000",
                "EqAR": "0.160",
                "BPS": "55.13",
                "ShOutFY": "47971989700",
                "TrShFY": "170724817",
            },
            {
                "DiscNo": "20261109568673",
                "DiscDate": "2026-11-09",
                "DocType": "2QFinancialStatements_Consolidated_IFRS",
                "CurPerType": "2Q",
                "CurFYEn": "2027-03-31",
                "CurPerSt": "2026-04-01",
                "CurPerEn": "2026-09-30",
                "NP": "364547000000",
                "TA": "20047515000000",
                "EqAR": "0.156",
                "BPS": "",
                "ShOutFY": "48071501700",
                "TrShFY": "163374056",
            },
        )
    )
    assert all(row is not None for row in rows)
    models = generate_financial_and_price_models(
        ticker="9434",
        documents=rows,
        adjusted_bars=[PriceBar(trading_day, 244.1, None)],
        adjustment_events=(),
        trading_dates=(trading_day,),
        expected_price_date=trading_day,
        sector="情報・通信業",
        as_of=evaluation_day,
        input_release=InputRelease(release_id="source-case", manifest_sha256="b" * 64),
        ttm_rules=DEFAULT_TTM_RULES,
    )
    financial = next(
        model.payload["financial"] for model in models if model.model_id == "company_financials"
    )
    assert financial["market_cap"] == pytest.approx(11_694_373_957_900.4)
    assert financial["pbr"] == pytest.approx(4.437614747687334)
    assert financial["same_state_equity_yen"] == pytest.approx(3_127_412_340_000.0)
    assert financial["book_equity"]["disclosure_number"] == "20260511521711"
    assert financial["book_equity"]["operands"]["shares_ex_treasury"] == 47_801_264_883

    discovery = project_discovery_row(
        "9434",
        models=(
            *models,
            GeneratedModel(
                model_id="market_context",
                subject="9434",
                calculation_revision="test-market",
                payload={"master": {"sector_33": "情報・通信業"}},
            ),
        ),
        metadata=GenerationMetadata(
            generation_id="test-generation",
            as_of=evaluation_day,
            evaluation_time=datetime(2026, 11, 24, tzinfo=UTC),
            inputs={
                "market": InputSnapshot(kind="lake_release", reference="test", sha256="a" * 64)
            },
        ),
    )
    assert discovery["book_equity"]["measurement_date"] == "2026-03-31"
    assert discovery["book_equity"]["method"] == "bps_times_reported_period_shares"
    capital = next(p for p in discovery["coordinates"] if p["path_id"] == "capital_productivity")
    assert capital["price_value"] == pytest.approx(1 / 4.437614747687334)
    # This remains reported TTM profit / measured book, not time-averaged ordinary ROE.
    assert capital["business_value"] == pytest.approx(566_551_000_000 / 2_635_283_732_999.79)


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
