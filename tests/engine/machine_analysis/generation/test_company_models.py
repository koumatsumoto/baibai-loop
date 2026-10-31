import zipfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from io import BytesIO

import pytest

from baibai_engine.machine_analysis.generation.contracts import (
    GeneratedModel,
    InputSnapshot,
    ModelGeneration,
)
from baibai_engine.machine_analysis.generation.financial_models import (
    generate_financial_and_price_models,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import TTMRules
from baibai_engine.machine_analysis.models.company_growth.models import InputRelease
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.csv import (
    parse_csv_zip_metric_record,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EdinetMetricRecord
from baibai_engine.machine_analysis.observations.financials.jquants_models import (
    JQuantsFinancialSummary,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar

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


def generate(
    *, documents, bars, growth_start=None, expected_price_date=ASOF, as_of=ASOF, edinet=None
):
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
        edinet=edinet,
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
    valuation = saved.result("company_financials", "1001").payload
    assert valuation["earnings_price"]["earnings_yield"] == profit / 800
    assert valuation["earnings_basis"] == "reported_profit_before_common_share_allocations"
    assert valuation["earnings_composition_basis"] == (
        "latest_reported_period_amounts_not_restatement_reconciled"
    )
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


def test_missing_price_does_not_erase_the_financial_history_or_invent_zero_yield():
    models = {model.model_id: model.payload for model in generate(documents=(document(),), bars=())}
    assert models["company_financials"]["financial"]["market_price_yen"] is None
    assert models["company_financials"]["financial"]["market_cap"] is None
    assert models["company_financials"]["financial"]["sales"] == 1000.0
    assert models["company_financials"]["financial"]["eps"] == 1.25
    assert models["company_financials"]["earnings_price"]["earnings_yield"] is None
    assert models["company_financials"]["earnings_price"]["unavailable_reason"] == "missing_price"
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


def test_optional_edinet_observations_reach_valuation_without_changing_y_growth_or_momentum():
    record = EdinetMetricRecord(
        ticker="1001",
        cash=100.0,
        debt=40.0,
        investment_securities=10.0,
        ocf_ttm=20.0,
        ebitda_ttm=30.0,
        fcf_ttm=12.0,
        capex_ttm=8.0,
        depreciation_and_amortization_ttm=5.0,
        consolidation_basis="consolidated",
        snapshot_asof=date(2026, 7, 1),
        snapshot_recorded_at_utc="2026-07-01T12:00:00Z",
        source_doc_id="S100TEST",
        document_type="120",
        source_period_start=date(2025, 4, 1),
        source_period_end=date(2026, 3, 31),
    )
    arguments = dict(documents=(document(),), bars=(PriceBar(ASOF, 10.0, None),))
    missing = generate(**arguments)
    present = generate(**arguments, edinet=record)
    financial = present[0].payload["financial"]
    assert financial["cash"] == 100
    assert financial["debt"] == 40
    assert financial["net_cash"] == 60
    assert financial["investment_securities"] == 10
    assert financial["asset_backed_ratio"] == pytest.approx(70 / 800)
    assert financial["edinet_reported_flows"]["amounts"]["free_cash_flow"] == 12
    assert financial["edinet_reported_flows"]["amounts"]["capex"] == 8
    assert financial["edinet_reported_flows"]["amounts"]["depreciation_and_amortization"] == 5
    assert financial["edinet_source_doc_id"] == "S100TEST"
    assert financial["edinet_snapshot_asof"] == "2026-07-01"
    assert financial["edinet_source_period_end"] == "2026-03-31"
    assert missing[0].payload["financial"]["debt"] is None
    assert present[0].payload["earnings_price"] == missing[0].payload["earnings_price"]
    assert present[1:] == missing[1:]
    assert "er_annual" not in financial
    assert present[0].calculation_revision.endswith("observations-v8-reported-flow-periods")
    with pytest.raises(ValueError, match="different ticker"):
        generate(**arguments, edinet=replace(record, ticker="1002"))


def test_edinet_entity_mismatch_keeps_provenance_without_filling_financial_values():
    record = EdinetMetricRecord(
        ticker="1001",
        cash=100.0,
        debt=40.0,
        consolidation_basis="non_consolidated",
        source_doc_id="S100MISMATCH",
    )
    result = generate(documents=(document(),), bars=(PriceBar(ASOF, 10.0, None),), edinet=record)[0]
    financial = result.payload["financial"]
    assert financial["cash"] is None
    assert financial["debt"] is None
    assert financial["edinet_source_doc_id"] == "S100MISMATCH"
    assert financial["edinet_failure_reasons"]


@pytest.mark.parametrize("price", [None, 0.0, -1.0])
@pytest.mark.parametrize("dividend", [0.0, 2.0])
def test_missing_or_invalid_price_keeps_financial_observations_and_only_nulls_price_ratios(
    price, dividend
):
    annual = replace(
        document(),
        dps_actual_annual=dividend,
        cash_eq=100.0,
        total_assets=1000.0,
        forecast_status="confirmed",
        forecast_eps=2.0,
        forecast_profit=160.0,
        dividend_forecast_status="confirmed",
        dps_forecast_annual=dividend,
    )
    record = EdinetMetricRecord(
        ticker="1001",
        cash=100.0,
        debt=40.0,
        fcf_ttm=12.0,
        consolidation_basis="consolidated",
        source_doc_id="S100RETAINED",
    )
    bars = () if price is None else (PriceBar(ASOF, price, None),)
    result = generate(documents=(annual,), bars=bars, edinet=record)[0].payload
    financial = result["financial"]
    assert financial["cash_eq"] == 100
    assert financial["total_assets"] == 1000
    assert financial["eps"] == 1.25
    assert financial["dps_actual_annual"] == dividend
    assert financial["dps_forecast_annual"] == dividend
    assert financial["forecast_eps_yen"] == 2.0
    assert financial["forecast_profit_yen"] == 160.0
    assert financial["debt"] == 40
    assert financial["cash"] == 100
    assert financial["edinet_reported_flows"]["amounts"]["free_cash_flow"] == 12
    assert financial["edinet_source_doc_id"] == "S100RETAINED"
    for field in (
        "market_price_yen",
        "market_cap",
        "per_forward",
        "per_trailing",
        "pbr",
        "ev_ebitda",
        "p_s",
        "pcfr",
        "dividend_yield",
        "fcf_yield",
        "net_cash_to_market_cap",
        "asset_backed_ratio",
    ):
        assert financial[field] is None, field
    assert result["earnings_price"]["earnings_yield"] is None
    assert result["earnings_price"]["unavailable_reason"] == (
        "missing_price" if price is None else "invalid_price"
    )
    assert result["earnings_price"]["price_yen"] == price


def reported_edinet(*, kind, start, end, ocf, capex, doc_id="S100SYNTHETIC"):
    context = "CurrentYearDuration" if kind in {"120", "130"} else "InterimDuration"
    instant = "CurrentYearInstant" if kind in {"120", "130"} else "InterimInstant"
    rows = [
        "要素ID\tコンテキストID\tユニットID\t値",
        f"jppfs_cor:NetCashProvidedByUsedInOperatingActivities\t{context}\tJPY\t{ocf}",
        f"jppfs_cor:PurchaseOfPropertyPlantAndEquipmentAndIntangibleAssetsInvCF\t{context}\tJPY\t{capex}",
        f"jppfs_cor:CashAndDeposits\t{instant}\tJPY\t100",
        f"jppfs_cor:ShortTermLoansPayable\t{instant}\tJPY\t100",
        f"jppfs_cor:OperatingIncome\t{context}\tJPY\t100000000",
        f"jppfs_cor:DepreciationAndAmortizationOpeCF\t{context}\tJPY\t10000000",
    ]
    content = BytesIO()
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("XBRL_TO_CSV/statement.csv", "\n".join(rows).encode("utf-16"))
    return parse_csv_zip_metric_record(
        ticker="1001",
        doc_id=doc_id,
        doc_type_code=kind,
        content=content.getvalue(),
        period_start=start,
        period_end=end,
        submit_datetime="2026-08-07T09:00:00+09:00",
    )


@pytest.mark.parametrize(
    ("source_doc_id", "ocf", "capex"),
    [
        ("S100YUMI", 812_758_000, 351_423_000),
        ("S100YVC0", 3_412_000_000, 8_175_000_000),
        ("S100XWND", None, None),
    ],
)
def test_interim_flows_are_reported_without_inventing_ttm_or_measurement_dates(
    source_doc_id, ocf, capex
):
    # #1638 の有限値を使った契約fixture。原本ZIPの再取得・実データ受入ではない。
    record = reported_edinet(
        kind="160",
        start=date(2026, 1, 1),
        end=date(2026, 12, 31),
        ocf=ocf,
        capex=capex,
        doc_id=source_doc_id,
    )
    financial = generate(
        documents=(document(),), bars=(PriceBar(ASOF, 10.0, None),), edinet=record
    )[0].payload["financial"]
    flows = financial["edinet_reported_flows"]
    assert flows["period_kind"] == "interim"
    assert flows["amounts"]["operating_cash_flow"] == ocf
    assert flows["amounts"]["capex"] == capex
    assert flows["amounts"]["free_cash_flow"] == (
        ocf - capex if ocf is not None and capex is not None else None
    )
    assert flows["measurement_period"] == {
        "start": None,
        "end": None,
        "reason": "fact_dates_not_retained",
    }
    assert flows["filing_fiscal_period"] == {"start": "2026-01-01", "end": "2026-12-31"}
    assert flows["annual_quality"]["free_cash_flow"] == (
        "approximated" if ocf is not None else "unavailable"
    )
    assert financial["fcf_yield"] is None
    assert financial["ev_ebitda"] is None
    assert not {"fcf_ttm", "edinet_ocf_ttm", "capex_ttm", "ebitda_ttm"}.intersection(financial)
    assert financial["ocf_ttm"] is None
    assert financial["ocf_ttm_period"] is None
    # The physical extracted record still retains exactly the reported amounts.
    assert record.ocf_ttm == ocf
    assert record.capex_ttm == capex


@pytest.mark.parametrize(
    ("kind", "start", "end", "quality"),
    [
        ("120", date(2025, 4, 1), date(2026, 3, 31), "exact"),
        ("130", date(2025, 4, 1), date(2026, 3, 31), "exact"),
        ("120", date(2025, 10, 1), date(2026, 3, 31), "approximated"),
        ("120", None, None, "approximated"),
        ("170", date(2026, 1, 1), date(2026, 12, 31), "approximated"),
    ],
)
def test_annual_interim_correction_and_unknown_period_keep_owner_quality(kind, start, end, quality):
    record = reported_edinet(kind=kind, start=start, end=end, ocf=100.0, capex=30.0)
    financial = generate(
        documents=(document(),), bars=(PriceBar(ASOF, 10.0, None),), edinet=record
    )[0].payload["financial"]
    flows = financial["edinet_reported_flows"]
    assert flows["amounts"]["free_cash_flow"] == 70.0
    assert flows["annual_quality"]["free_cash_flow"] == quality
    assert flows["filing_fiscal_period"] == {
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
    }
    assert flows["measurement_period"]["start"] is None
    assert flows["measurement_period"]["end"] is None
    assert financial["fcf_yield"] == (70.0 / 800 if quality == "exact" else None)
    assert (financial["ev_ebitda"] is not None) == (quality == "exact")


def cash_flow_documents():
    annual = replace(document(), cfo=5_297_000_000.0)
    current = replace(
        annual,
        disclosed_at=date(2026, 8, 6),
        disclosure_number="current-quarter",
        fiscal_period="Q1",
        fiscal_year_end=date(2027, 3, 31),
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        document_type="1QFinancialStatements_Consolidated_JP",
        cfo=-5_602_092_000.0,
    )
    prior = replace(
        current,
        disclosed_at=date(2025, 8, 7),
        disclosure_number="prior-quarter",
        fiscal_year_end=date(2026, 3, 31),
        period_start=date(2025, 4, 1),
        period_end=date(2025, 6, 30),
        cfo=924_543_000.0,
    )
    return annual, current, prior


def test_march_reported_fcf_and_june_trailing_cfo_keep_distinct_periods_and_precision():
    record = reported_edinet(
        kind="120",
        start=date(2025, 4, 1),
        end=date(2026, 3, 31),
        ocf=5_297_471_000.0,
        capex=2_074_971_000.0,
        doc_id="S100YDEK",
    )
    financial = generate(
        documents=cash_flow_documents(), bars=(PriceBar(ASOF, 10.0, None),), edinet=record
    )[0].payload["financial"]
    assert financial["ocf_ttm"] == -1_229_635_000.0
    assert {
        key: value for key, value in financial["ocf_ttm_period"].items() if key != "components"
    } == {
        "start": "2025-07-01",
        "end": "2026-06-30",
        "method": "fy_plus_current_ytd_minus_prior_ytd",
        "statement_basis": "consolidated",
        "accounting_standard": "JP",
    }
    components = financial["ocf_ttm_period"]["components"]
    assert [row["disclosure_number"] for row in components] == [
        "current-quarter",
        "synthetic-annual",
        "prior-quarter",
    ]
    assert components[1]["amount_yen"] == 5_297_000_000.0
    assert components[1]["period_start"] == "2025-04-01"
    assert components[1]["period_end"] == "2026-03-31"
    assert components[1]["disclosed_at"] == "2026-05-15"
    flows = financial["edinet_reported_flows"]
    assert flows["amounts"]["operating_cash_flow"] == 5_297_471_000.0
    assert flows["amounts"]["free_cash_flow"] == 3_222_500_000.0
    assert flows["filing_fiscal_period"]["end"] == "2026-03-31"
    assert flows["annual_quality"]["free_cash_flow"] == "exact"
    assert flows["measurement_period"]["end"] is None
    assert financial["fcf_yield"] == 3_222_500_000.0 / 800


@pytest.mark.parametrize("barrier", ["basis_change", "missing_correction", "changed_correction"])
def test_cfo_interval_uses_the_same_basis_and_correction_as_its_value(barrier):
    annual, current, prior = cash_flow_documents()
    if barrier == "basis_change":
        prior = replace(prior, document_type="1QFinancialStatements_NonConsolidated_JP")
        documents = annual, current, prior
    else:
        correction = replace(
            current,
            disclosed_at=date(2026, 8, 10),
            disclosure_number="corrected-quarter",
            cfo=None if barrier == "missing_correction" else -6_000_000_000.0,
        )
        documents = annual, current, prior, correction
    financial = generate(documents=documents, bars=())[0].payload["financial"]
    if barrier == "changed_correction":
        assert financial["ocf_ttm"] == 5_297_000_000.0 - 6_000_000_000.0 - 924_543_000.0
        assert financial["ocf_ttm_period"]["end"] == "2026-06-30"
    else:
        assert financial["ocf_ttm"] is None
        assert financial["ocf_ttm_period"] is None


def test_reported_annual_cfo_interval_keeps_known_dates_and_source():
    financial = generate(documents=(replace(document(), cfo=100.0),), bars=())[0].payload[
        "financial"
    ]
    period = financial["ocf_ttm_period"]
    assert period["start"] == "2025-04-01"
    assert period["end"] == "2026-03-31"
    assert period["method"] == "reported_annual"
    assert len(period["components"]) == 1
    assert period["components"][0]["coefficient"] == 1
    assert period["components"][0]["disclosure_number"] == "synthetic-annual"
    assert period["components"][0]["amount_yen"] == financial["ocf_ttm"] == 100.0


def test_saved_old_model_retains_its_payload_without_new_period_defaults():
    old = GeneratedModel(
        model_id="company_financials",
        subject="1001",
        calculation_revision="continuous-annual-input-v45-observations-v7",
        payload={"financial": {"edinet_ocf_ttm": 812_758_000.0, "fcf_ttm": 461_335_000.0}},
    )
    saved = old.model_dump_json()
    assert GeneratedModel.model_validate_json(saved).model_dump_json() == saved
    assert "edinet_reported_flows" not in saved
    assert "ocf_ttm_period" not in saved
