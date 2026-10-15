"""Stock Analysisと同runの候補文脈を表示する。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from baibai_engine.read_api import (
    anchor_gap_pct,
    interpreted_next_earnings_status,
    machine_valuation_from_analysis,
    reinvestment_coordinates,
)
from baibai_engine.read_api.er_calibration_context import (
    ErCalibrationContextArtifact,
    candidate_er_bands,
)
from baibai_web.readmodel.builders.presentation import number, text
from baibai_web.readmodel.models import (
    CandidateDiscoverySummaryView,
    ErLevelCalibrationBandView,
    ErLevelCalibrationBasisView,
    ErLevelCalibrationContextView,
    ErLevelCalibrationCoverageView,
    ErLevelCalibrationHorizonView,
    ErLevelCalibrationStatsView,
    ExternalAssessmentSecurityView,
    ScreeningHistoryRunView,
    ScreeningRunView,
    ScreeningView,
    StockAnalysisRowView,
    StockCandidateContextView,
)
from baibai_web.readmodel.stocks.presentation import (
    _held_and_reserved_tickers,
    _portfolio_state,
)
from baibai_web.readmodel.stocks.research import _proposal_summary_view
from baibai_web.sources.db_sources import DbScreeningSource
from baibai_web.sources.protocols import (
    ExternalAssessmentSource,
    LedgerSource,
    MarketPriceSource,
    ResearchSource,
    ScreeningSource,
)
from baibai_web.sources.types import ScreeningRunRecord

_JST = ZoneInfo("Asia/Tokyo")

_NUMERIC_FIELDS = (
    "market_cap_oku",
    "avg_turnover_oku",
    "per_trailing",
    "per_forward",
    "pbr",
    "ev_ebitda",
    "p_s",
    "pcfr",
    "price_change_20d",
    "gap_from_52w_low",
    "sector_relative_strength_percentile",
)

_METRIC_FIELDS = (
    "dividend_yield",
    "dividend_split_factor",
    "normalized_per_3fy",
    "er_annual",
    "er_reversion_annual",
    "er_carry_annual",
    "net_cash_to_market_cap",
    "fcf_yield",
    "ocf_yield",
    "equity_ratio",
    "sales_yoy",
    "operating_profit_yoy",
    "margin_long_to_adv",
    "margin_short_to_adv",
    "margin_long_share",
    "margin_long_delta_26w",
    "margin_std_long_share",
)

_METRIC_TEXT_FIELDS = ("dividend_basis",)

_STALE_RUN_AGE = timedelta(days=7)


def build_screening(
    screening: ScreeningSource,
    ledger: LedgerSource,
    research: ResearchSource,
    er_level_calibration: ErCalibrationContextArtifact | None = None,
    *,
    external_assessment: ExternalAssessmentSource | None = None,
    market: MarketPriceSource | None = None,
    external_cutoff: datetime | None = None,
) -> ScreeningView:
    """Build the latest Stock Analysis view with portfolio/research annotations."""

    run = screening.latest_run()
    cutoff = external_cutoff or datetime.now(_JST)
    held, reserved = _held_and_reserved_tickers(ledger)
    revisions = research.revisions()
    researched = {item.ticker for item in revisions}
    latest_revisions = {item.ticker: item for item in reversed(revisions)}
    analyzed = set() if run is None else {str(row["ticker"]) for row in run.rows}
    external_tickers = (
        [] if external_assessment is None else external_assessment.observed_tickers(cutoff=cutoff)
    )
    identities = (
        {}
        if market is None or not external_tickers
        else market.security_identities(as_of=cutoff.date())
    )
    external_securities = [
        ExternalAssessmentSecurityView(
            ticker=ticker,
            name=latest_revisions[ticker].company_name
            if ticker in latest_revisions
            else text(identities.get(ticker, {}).get("name")) or None,
            sector_33=latest_revisions[ticker].sector
            if ticker in latest_revisions
            else text(identities.get(ticker, {}).get("sector_33")),
            portfolio_state=_portfolio_state(ticker, held=held, reserved=reserved),
            has_research=ticker in researched,
        )
        for ticker in external_tickers
        if ticker not in analyzed
    ]
    applicable_calibration = _calibration_for_run(run, er_level_calibration)
    proposals = [_proposal_summary_view(item) for item in research.proposals()]
    if run is None:
        return ScreeningView(
            run=None,
            stock_analyses=[],
            candidate_discovery=None,
            investment_proposals=proposals,
            er_level_calibration=None,
            external_assessment_securities=external_securities,
            external_assessment_cutoff=cutoff if external_securities else None,
        )
    today = datetime.now(_JST).date()
    return ScreeningView(
        run=_screening_run_view(run, today=today),
        stock_analyses=[
            _stock_analysis_row_view(
                row,
                held=held,
                reserved=reserved,
                researched=researched,
                er_level_calibration=applicable_calibration,
            )
            for row in run.rows
        ],
        candidate_discovery=_candidate_summary(run),
        investment_proposals=proposals,
        er_level_calibration=_er_level_calibration_view(applicable_calibration),
        external_assessment_securities=external_securities,
        external_assessment_cutoff=cutoff if external_securities else None,
    )


def _calibration_for_run(
    run: ScreeningRunRecord | None,
    context: ErCalibrationContextArtifact | None,
) -> ErCalibrationContextArtifact | None:
    """Bind historical E[r] bands to the exact method of the displayed run."""

    if run is None or context is None:
        return None
    if (
        run.screening_rules_hash != context.screening_rules_hash
        or run.er_model_version != context.er_model_version
    ):
        return None
    return context


def _candidate_summary(run: ScreeningRunRecord) -> CandidateDiscoverySummaryView:
    summary = run.candidate_discovery
    diagnostics = summary["diagnostics"]
    if not isinstance(diagnostics, Mapping):
        raise ValueError("candidate diagnostics must be an object")
    return CandidateDiscoverySummaryView.model_validate(
        {"method": summary["method"], **diagnostics}
    )


def build_screening_history_run(
    screening: DbScreeningSource,
    ledger: LedgerSource,
    research: ResearchSource,
    *,
    as_of: date,
) -> ScreeningHistoryRunView | None:
    """Build one retained run with the same current-state annotations as latest."""

    run = screening.run_as_of(as_of)
    if run is None:
        return None
    held, reserved = _held_and_reserved_tickers(ledger)
    researched = {item.ticker for item in research.revisions()}
    return ScreeningHistoryRunView(
        run=_screening_run_view(run, today=datetime.now(_JST).date()),
        rows=[
            _stock_analysis_row_view(
                row,
                held=held,
                reserved=reserved,
                researched=researched,
            )
            for row in run.rows
        ],
    )


def _data_quality_flags(row: Mapping[str, object], metrics: Mapping[str, object]) -> list[str]:
    """Surface stale/incomplete data so no metric is trusted silently in the table."""

    flags: list[str] = []
    ttm_quality = row.get("ttm_quality")
    if isinstance(ttm_quality, Mapping) and any(value != "exact" for value in ttm_quality.values()):
        flags.append("TTM非exact")
    if metrics.get("edinet_failure_reasons"):
        flags.append("EDINET失敗")
    lag = metrics.get("bs_carry_forward_lag_days")
    if isinstance(lag, (int, float)) and not isinstance(lag, bool) and lag > 0:
        flags.append("BS前期繰越")
    if row.get("freshness_warnings"):
        flags.append("鮮度warning")
    if metrics.get("forecast_special_gain_flag") is True:
        flags.append("一時益予想")
    if metrics.get("stale_fin_flag") is True:
        flags.append("予定日到来・財務未照合")
    dividend_basis = metrics.get("dividend_basis")
    if isinstance(dividend_basis, str) and (
        dividend_basis.startswith("unresolved_") or dividend_basis == "unavailable"
    ):
        flags.append(dividend_basis)
    return flags


def _screening_run_view(run: ScreeningRunRecord, *, today: date) -> ScreeningRunView:
    return ScreeningRunView(
        run_id=run.run_id,
        as_of=run.as_of,
        generated_at=run.generated_at,
        universe_size=run.universe_size,
        analyzed_security_count=len(run.rows),
        run_revision_id=run.run_revision_id,
        stale=run.as_of <= today - _STALE_RUN_AGE,
    )


def _stock_analysis_row_view(
    row: Mapping[str, object],
    *,
    held: set[str],
    reserved: set[str],
    researched: set[str],
    er_level_calibration: ErCalibrationContextArtifact | None = None,
) -> StockAnalysisRowView:
    flags_raw = row.get("jpx_flags")
    coordinates = reinvestment_coordinates(row)
    quality_raw = row.get("ttm_quality")
    quality = quality_raw if isinstance(quality_raw, Mapping) else {}
    ticker = str(row.get("ticker", ""))
    metrics_raw = row.get("metrics")
    metrics = metrics_raw if isinstance(metrics_raw, Mapping) else {}
    values = {name: number(row.get(name)) for name in _NUMERIC_FIELDS}
    values.update({name: number(metrics.get(name)) for name in _METRIC_FIELDS})
    # 文字列の annotation は `number` を通すと必ず None になるので別に詰める。
    # `dividend_basis` は「配当利回りが空である理由」を持つ唯一の field で、
    # unresolved_split_basis を無配と読み違えないために Stock Analysis 一覧まで届ける必要がある。
    text_values = {name: text(metrics.get(name)) for name in _METRIC_TEXT_FIELDS}
    flags = _data_quality_flags(row, metrics)
    valuation = machine_valuation_from_analysis(row)
    quintile: int | None = None
    meets_hurdle = False
    er_annual = values.get("er_annual")
    if er_annual is not None and er_level_calibration is not None:
        horizon = next(
            item
            for item in er_level_calibration.horizons
            if item.horizon == er_level_calibration.reference_horizon
        )
        bands = candidate_er_bands(horizon, er_annual)
        quintile = bands[0].quintile
        meets_hurdle = len(bands) > 1
    return StockAnalysisRowView(
        candidate=StockCandidateContextView.model_validate(row.get("candidate")),
        listing_span_days=number(row.get("listing_span_days")),
        jpx_flags=list(flags_raw) if isinstance(flags_raw, list | tuple) else None,
        forecast_special_gain_flag=_optional_bool(metrics.get("forecast_special_gain_flag")),
        forecast_full_year_loss_flag=_optional_bool(metrics.get("forecast_full_year_loss_flag")),
        investment_securities=number(metrics.get("investment_securities")),
        asset_backed_ratio=number(metrics.get("asset_backed_ratio")),
        p_s_sector_gap=coordinates["p_s_sector_gap"],
        operating_return_on_capital_proxy=coordinates["operating_return_on_capital_proxy"],
        operating_margin=coordinates["operating_margin"],
        bs_carry_forward_fields=_optional_text(metrics.get("bs_carry_forward_fields")),
        bs_carry_forward_lag_days=number(metrics.get("bs_carry_forward_lag_days")),
        edinet_failure_reasons=_optional_text(metrics.get("edinet_failure_reasons")),
        stale_fin_flag=_optional_bool(metrics.get("stale_fin_flag")),
        ttm_quality_ev_ebitda=quality.get("ev_ebitda"),
        ttm_quality_fcf=quality.get("fcf_yield"),
        next_earnings_status=interpreted_next_earnings_status(
            _optional_text(metrics.get("next_earnings_status"))
        ),
        next_earnings_estimated_date=_optional_text(metrics.get("next_earnings_estimated_date")),
        tse_capital_policy_status=_optional_text(metrics.get("tse_capital_policy_status")),
        large_holding_filing_within_lookback=_optional_bool(
            metrics.get("large_holding_event_recent")
        ),
        tender_offer_filing_within_lookback=_optional_bool(
            metrics.get("tender_offer_event_recent")
        ),
        fv_sector_median_yen=number(metrics.get("fv_sector_median_yen")),
        fv_self_range_yen=number(metrics.get("fv_self_range_yen")),
        er_origin=_optional_text(metrics.get("er_origin")),
        ticker=ticker,
        name=text(row.get("name")),
        sector_33=text(row.get("sector_33")),
        next_earnings_date=text(row.get("next_earnings_date")),
        margin_week_end=text(metrics.get("margin_week_end")),
        data_quality_flags=flags,
        portfolio_state=_portfolio_state(ticker, held=held, reserved=reserved),
        has_research=ticker in researched,
        fair_value_anchor_yen=valuation.sector_median_anchor_yen,
        fair_value_gap_pct=anchor_gap_pct(valuation),
        net_share_change_yoy=valuation.net_share_change_yoy,
        carry_missing_assumption=valuation.carry_missing_assumption,
        er_model_version=valuation.model_version,
        er_unit=valuation.unit,
        er_anchor_metrics=valuation.anchor_metrics,
        er_assumptions=valuation.assumptions,
        er_level_quintile=quintile,
        er_meets_8_5pct_band=meets_hurdle,
        **values,
        **text_values,
    )


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _er_level_calibration_view(
    context: ErCalibrationContextArtifact | None,
) -> ErLevelCalibrationContextView | None:
    if context is None:
        return None
    return ErLevelCalibrationContextView(
        interpretation=context.interpretation,
        required_asofs=context.required_asofs,
        evidence_complete=context.evidence_complete,
        evidence_blocking_reasons=context.evidence_blocking_reasons,
        generated_at=context.generated_at,
        valid_through=context.valid_through,
        reference_horizon=context.reference_horizon,
        screening_rules_hash=context.screening_rules_hash,
        er_model_version=context.er_model_version,
        primary_realized_basis=context.primary_realized_basis,
        secondary_realized_basis=context.secondary_realized_basis,
        trap_basis=context.trap_basis,
        horizons=[
            ErLevelCalibrationHorizonView(
                horizon=item.horizon,
                as_of_start=item.asof_start,
                as_of_end=item.asof_end,
                cohort_count=item.cohort_count,
                bands=[
                    ErLevelCalibrationBandView(
                        band_id=band.band_id,
                        quintile=band.quintile,
                        lower_er_annual=band.lower_er_annual,
                        upper_er_annual=band.upper_er_annual,
                        median_predicted_er_annual=band.median_predicted_er_annual,
                        cohort_count=band.cohort_count,
                        median_n=band.median_n,
                        bases=[
                            ErLevelCalibrationBasisView(
                                basis=basis.basis,
                                coverage=ErLevelCalibrationCoverageView(
                                    **basis.coverage.model_dump()
                                ),
                                observed_cohort_count=basis.observed_cohort_count,
                                median_n=basis.median_n,
                                ticker_equal=ErLevelCalibrationStatsView(
                                    median=basis.ticker_equal.median,
                                    q25=basis.ticker_equal.q25,
                                    q10=basis.ticker_equal.q10,
                                    trap_rate=basis.ticker_equal.trap_rate,
                                    n=basis.ticker_equal.n,
                                )
                                if basis.ticker_equal is not None
                                else None,
                                cohort_equal=ErLevelCalibrationStatsView(
                                    median=basis.cohort_equal.median,
                                    q25=basis.cohort_equal.q25,
                                    q10=basis.cohort_equal.q10,
                                    trap_rate=basis.cohort_equal.trap_rate,
                                    n=basis.cohort_equal.n,
                                )
                                if basis.cohort_equal is not None
                                else None,
                            )
                            for basis in band.bases
                        ],
                    )
                    for band in item.bands
                ],
            )
            for item in context.horizons
        ],
    )
