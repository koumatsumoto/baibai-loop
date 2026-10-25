from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from baibai_engine.machine_analysis.company_growth.models import InputRelease
from baibai_engine.machine_analysis.company_models import generate_financial_and_price_models
from baibai_engine.machine_analysis.company_valuation.metrics.models import TTMRules
from baibai_engine.machine_analysis.model_generation import InputSnapshot, ModelGeneration
from baibai_engine.machine_analysis.screening.model_projection import select_generation
from baibai_engine.machine_analysis.screening.views import TwoViewPolicy
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.price_models import PriceBar

ASOF = date(2026, 8, 31)
CALENDAR = tuple(ASOF - timedelta(days=252 - index) for index in range(253))
RELEASE = InputRelease(release_id="synthetic-release", manifest_sha256="a" * 64)
RULES = TTMRules(
    full_year_min_days=330,
    full_year_max_days=370,
    period_length_tolerance_ratio=0.1,
    period_end_tolerance_days=45,
)


def document(*, ticker="1001", profit=100.0):
    return JQuantsFinancialSummary(
        ticker=ticker,
        disclosed_at=date(2026, 5, 15),
        fiscal_period="FY",
        fiscal_year_end=date(2026, 3, 31),
        period_start=date(2025, 4, 1),
        period_end=date(2026, 3, 31),
        document_type="FYFinancialStatements_Consolidated_JP",
        disclosure_number="synthetic-annual",
        profit=profit,
        sales=1000.0,
        shares_outstanding=100.0,
        treasury_shares=20.0,
    )


def generate(*, documents, bars, growth_start=None, expected_price_date=ASOF, as_of=ASOF):
    return generate_financial_and_price_models(
        ticker="1001",
        documents=documents,
        adjusted_bars=bars,
        adjustment_events=(),
        trading_dates=CALENDAR,
        expected_price_date=expected_price_date,
        sector=None,
        as_of=as_of,
        input_release=RELEASE,
        ttm_rules=RULES,
        growth_start=growth_start,
    )


@pytest.mark.parametrize("profit", [-100.0, 0.0, 100.0])
def test_owner_results_round_trip_into_selection_without_legacy_er(profit):
    models = generate(
        documents=(document(profit=profit),),
        bars=[
            PriceBar(ASOF + timedelta(days=1), 999.0, None),
            *(PriceBar(day, 10.0, None) for day in reversed(CALENDAR)),
        ],
    )
    generated = ModelGeneration(
        generation_id="synthetic-generation",
        as_of=ASOF,
        evaluation_time=datetime(2026, 9, 1, tzinfo=UTC),
        inputs={
            "market": InputSnapshot(
                kind="lake_release", reference=RELEASE.release_id, sha256=RELEASE.manifest_sha256
            )
        },
        models=models,
    )
    saved = ModelGeneration.model_validate_json(generated.model_dump_json())
    assert saved == generated
    valuation = saved.result("company_valuation", "1001").payload
    assert valuation["earnings_price"]["earnings_yield"] == profit / 800
    assert valuation["earnings_price"]["price_date"] == ASOF.isoformat()
    assert valuation["earnings_price"]["profit_components"][0]["disclosure_number"] == (
        "synthetic-annual"
    )
    assert valuation["financial"]["market_cap"] == 800
    assert "er_annual" not in valuation["financial"]
    assert not {
        "eps_yoy",
        "sales_yoy",
        "operating_profit_yoy",
        "cfo_yoy",
        "operating_profit_loss_narrowing",
    }.intersection(valuation["financial"])
    growth = saved.result("company_growth", "1001").payload
    assert growth["status"] == "available"
    assert growth["input_release"]["release_id"] == RELEASE.release_id
    technical = saved.result("technical_analysis", "1001").payload
    assert technical["price_momentum"]["value"] == 0
    assert technical["moving_averages"]["sma_200bar"]["value"] == 10
    selection = select_generation(
        saved, tickers=("1001",), policy=TwoViewPolicy(earnings_limit=1, combined_limit=1)
    )
    assert selection.result.tickers == ("1001",)
    assert selection.result.views[0].companies[0].ranking_value == profit / 800


def test_missing_price_does_not_erase_the_financial_history_or_invent_zero_yield():
    models = {model.model_id: model.payload for model in generate(documents=(document(),), bars=())}
    assert models["company_valuation"]["financial"] is None
    assert models["company_valuation"]["earnings_price"]["earnings_yield"] is None
    assert models["company_valuation"]["earnings_price"]["unavailable_reason"] == "missing_price"
    assert models["company_growth"]["status"] == "available"
    assert models["technical_analysis"]["price_momentum"]["status"] == "missing_endpoint"


def test_shared_input_cannot_mix_another_company_into_the_generated_models():
    with pytest.raises(ValueError, match="different ticker"):
        generate(documents=(document(ticker="1002"),), bars=())


def test_growth_display_range_does_not_truncate_valuation_inputs_or_retained_coverage():
    full = generate(documents=(document(),), bars=(PriceBar(ASOF, 10.0, None),))
    bounded = generate(
        documents=(document(),),
        bars=(PriceBar(ASOF, 10.0, None),),
        growth_start=date(2026, 4, 1),
    )
    assert full[0] == bounded[0]
    assert full[2] == bounded[2]
    assert full[1].payload["periods"]
    assert bounded[1].payload["periods"] == []
    assert full[1].payload["coverage"] == bounded[1].payload["coverage"]
    assert bounded[1].payload["requested_start"] == "2026-04-01"


def test_forecast_ratios_keep_their_numeric_inputs_not_only_warning_flags():
    forecast = replace(
        document(),
        forecast_status="confirmed",
        forecast_eps=2.0,
        forecast_profit=160.0,
        forecast_ordinary_profit=-10.0,
    )
    valuation = generate(documents=(forecast,), bars=(PriceBar(ASOF, 10.0, None),))[0]
    financial = valuation.payload["financial"]
    assert financial["forecast_eps_yen"] == 2.0
    assert financial["forecast_profit_yen"] == 160.0
    assert financial["forecast_ordinary_profit_yen"] == -10.0
    assert financial["per_forward"] == 5.0
    assert financial["forecast_special_gain_flag"] is True
    assert financial["forecast_full_year_loss_flag"] is True


@pytest.mark.parametrize("profit", [0.0, 100.0])
@pytest.mark.parametrize("expected", [ASOF, None])
def test_unconfirmed_current_price_keeps_raw_facts_but_not_candidate_yield(profit, expected):
    old_day = ASOF - timedelta(days=3)
    valuation = generate(
        documents=(document(profit=profit),),
        bars=(PriceBar(old_day, 10.0, None),),
        expected_price_date=expected,
    )[0].payload
    earnings = valuation["earnings_price"]
    assert earnings["earnings_yield"] is None
    assert earnings["unavailable_reason"] == (
        "insufficient_calendar" if expected is None else "stale_price"
    )
    assert earnings["price_date"] == old_day.isoformat()
    assert earnings["profit_ttm_yen"] == profit
    assert earnings["market_cap_yen"] == 800
    assert valuation["financial"]["market_cap"] == 800


def test_latest_trading_close_remains_valid_on_a_later_nontrading_asof():
    friday = ASOF - timedelta(days=3)
    earnings = generate(
        documents=(document(),),
        bars=(PriceBar(friday, 10.0, None),),
        expected_price_date=friday,
        as_of=ASOF - timedelta(days=1),
    )[0].payload["earnings_price"]
    assert earnings["earnings_yield"] == 0.125
