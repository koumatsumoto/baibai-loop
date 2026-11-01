"""Historical as-of panel for Stock Analysis and candidate replay.

保存済み資料を基準日で絞る回顧的な再計算。取得時点の全 vintage を復元した
厳密な point-in-time データセットではない。

旧run契約の部品 (`build_metrics` / `stock_analysis_payload` /
`build_candidate_contexts`) を使う。現行のモデル生成・候補政策の成績ではない。
旧runとの相違点は入力の中立化:

- 市場環境判断は候補順位の入力にしない
- 過去の候補集合や判断を過去cohortの順位へ混入させない
- JPX 規制 flag は保存済みの同日snapshotだけを使い、source不足はdiagnosticsへ残す
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from baibai_engine.machine_analysis.historical_evaluation.estimates import estimate_expected_return
from baibai_engine.machine_analysis.historical_evaluation.horizons import STALE_PRICE_MAX_LAG_DAYS
from baibai_engine.machine_analysis.historical_evaluation.identity import rules_contract_hash
from baibai_engine.machine_analysis.historical_evaluation.input_windows import (
    BARS_INPUT_WINDOW_DAYS,
    FIN_INPUT_WINDOW_DAYS,
    NORMALIZED_EPS_HISTORY_WINDOW_DAYS,
    SHAREHOLDER_RETURN_HISTORY_WINDOW_DAYS,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.annual import (
    build_normalized_profit_signals,
    build_profitability_level_signals,
    build_shareholder_return_change_signals,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.history import (
    VALUATION_HISTORY_SESSIONS,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.margin_inputs import (
    read_margin_supply_demand_inputs,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.market import (
    MARKET_CONTEXT_CALCULATION_REVISION,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.metric_builder import (
    build_metrics,
    group_adjustment_events_by_ticker,
    group_bars_by_ticker,
    group_summaries_by_ticker,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.relative import (
    SECTOR_MEDIAN_BASIS_MARKET,
    VALUATION_METRICS,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.rule_config import (
    ScreeningRules,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.schema import (
    StockAnalysis,
)
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.universe import (
    POLICY_EXCLUSION_REASONS,
    UNIVERSE_SCOPE_REVISION,
    build_universe,
    candidate_comparison_population,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.capital import (
    build_shares_outstanding_index,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.financial_snapshot import (
    VALUATION_CALCULATION_REVISION,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_reader import read_jpx_regulations
from baibai_engine.machine_analysis.observations.financials.edinet_metrics.store import (
    read_edinet_metrics,
)
from baibai_engine.machine_analysis.observations.financials.metric_quality import TTMQuality
from baibai_engine.machine_analysis.observations.financials.reader import (
    fin_summaries_readable_from,
    read_fin_summaries,
)
from baibai_engine.machine_analysis.observations.prices.price_reader import (
    read_adjustment_factor_bars,
    read_daily_bars,
)
from baibai_engine.machine_analysis.observations.stocks.reader import (
    read_eq_master_asof,
)

from .legacy_screening.discovery.candidates import (
    build_candidate_contexts,
    candidate_discovery_method_hash,
)
from .legacy_screening.screening_run_serializer import (
    stock_analysis_payload,
)
from .legacy_screening.stock_analysis_builder import (
    build_stock_analysis,
    expected_return_inputs,
)

PanelVariant = Literal["production", "pre2019_self_range_375"]
CandidateDiscoveryInputStatus = Literal["complete", "incomplete", "unavailable"]
PopulationCoverageStatus = Literal[
    "evaluated", "priced_master_without_universe", "master_without_universe_unpriced"
]
PANEL_CALCULATION_REVISION = "adjoining-annual-fy-signals-v1"


@dataclass(frozen=True, slots=True)
class PanelBuildPolicy:
    """Input contract for a calibration panel build."""

    variant: PanelVariant
    valuation_history_sessions: int
    bars_input_window_days: int
    production_authority: bool


PRODUCTION_PANEL_POLICY = PanelBuildPolicy(
    variant="production",
    valuation_history_sessions=VALUATION_HISTORY_SESSIONS,
    bars_input_window_days=BARS_INPUT_WINDOW_DAYS,
    production_authority=True,
)
PRE2019_SELF_RANGE_POLICY = PanelBuildPolicy(
    variant="pre2019_self_range_375",
    valuation_history_sessions=375,
    bars_input_window_days=600,
    production_authority=False,
)
PANEL_BUILD_POLICIES: dict[PanelVariant, PanelBuildPolicy] = {
    policy.variant: policy for policy in (PRODUCTION_PANEL_POLICY, PRE2019_SELF_RANGE_POLICY)
}


class CalibrationError(RuntimeError):
    """Raised when the cache cannot serve a point-in-time panel build."""


def rules_content_hash(
    rules: ScreeningRules, policy: PanelBuildPolicy = PRODUCTION_PANEL_POLICY
) -> str:
    """screening rules と panel input contract の semantic identity。"""
    return rules_contract_hash(
        rules.model_dump_json(),
        candidate_discovery_method_hash=candidate_discovery_method_hash(
            rules.candidate_discovery,
            required_jpx_flags=rules.universe.required_jpx_flags,
        ),
        universe_scope_revision=UNIVERSE_SCOPE_REVISION,
        panel_calculation_revision=PANEL_CALCULATION_REVISION,
        valuation_calculation_revision=VALUATION_CALCULATION_REVISION,
        market_context_calculation_revision=MARKET_CONTEXT_CALCULATION_REVISION,
        variant=policy.variant,
        valuation_history_sessions=policy.valuation_history_sessions,
        bars_input_window_days=policy.bars_input_window_days,
        production_authority=policy.production_authority,
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelRow:
    asof: str
    ticker: str
    sector_33: str
    in_population: bool
    market_cap_oku: float | None
    avg_turnover_oku: float | None
    price_history_span_days: int | None
    close: float | None
    per_forward: float | None
    per_trailing: float | None
    pbr: float | None
    ev_ebitda: float | None
    p_s: float | None
    pcfr: float | None
    ocf_yield: float | None
    fcf_yield: float | None
    net_cash_to_market_cap: float | None
    cash_to_market_cap: float | None
    investment_securities: float | None
    asset_backed_ratio: float | None
    equity_ratio: float | None
    dividend_yield: float | None
    eps_yoy: float | None
    sales_yoy: float | None
    operating_profit_yoy: float | None
    cfo_yoy: float | None
    accruals_to_assets: float | None
    net_share_change_yoy: float | None
    tradable_share_change_yoy: float | None
    ttm_quality_per_trailing: str
    ttm_quality_ocf_yield: str
    price_change_60d: float | None
    gap_from_52w_low: float | None
    price_history_coverage_750d: float | None
    smg_per_forward: float | None
    smg_per_trailing: float | None
    smg_pbr: float | None
    smg_ev_ebitda: float | None
    smg_p_s: float | None
    srp_per_forward: float | None
    srp_per_trailing: float | None
    srp_pbr: float | None
    srp_ev_ebitda: float | None
    srp_p_s: float | None
    er_annual: float | None
    er_reversion_annual: float | None
    er_carry_annual: float | None
    er_upside_capped: float | None
    # Supply/demand from the exchange's published all-issues margin balances, joined
    # at the publication lag of whichever series published the balance date (see
    # `market.margin_reader.published_margin_balance_dates`). Axes that use the daily
    # series' frequency rather than its balances must use separately named
    # calibration fields. Carried on the panel so the axes can be measured against
    # forward returns before any of them is allowed to change a rule.
    # The balance date behind the four numbers. Carried so a panel row states how
    # old its positioning read is: the balance is published days later, and a store
    # with a gap would otherwise show plausible axes with no trace of which balance
    # date they came from.
    margin_balance_date: str | None
    margin_long_to_adv: float | None
    margin_long_share: float | None
    margin_long_delta_26w: float | None
    margin_std_long_share: float | None
    # 保存済み較正panelのwire名。意味はCandidate Discoveryのnomination union所属。
    in_nomination_union: bool
    valuation_approaches: str
    valuation_approach_ranks: str
    population_coverage_status: PopulationCoverageStatus = "evaluated"
    self_range_degraded: bool = False
    dps_streak_up: bool | None = None
    dps_yoy_latest: float | None = None
    dps_guidance_up: bool | None = None
    dividend_initiation: bool | None = None
    share_count_reduction_streak: int | None = None
    shareholder_return_change: bool | None = None
    margin_short_to_adv: float | None = None
    realized_volatility_60d: float | None = None
    normalized_per_3fy: float | None = None
    normalized_per_5fy: float | None = None
    self_range_observed_sessions: int = 0
    operating_profit_to_assets: float | None = None
    operating_margin: float | None = None
    asset_turnover: float | None = None
    # `smg_*` のうち、業種の母数が足りず市場中央値から作られた軸を `|` で並べる。
    # 薄い業種は市場より低倍率へ寄るので、この素性が無いと gap の符号を業種の割安と
    # 読むか業種構成と読むかを分けられない。metric 名は
    # `legacy_screening.relative.VALUATION_METRICS` と同じ語彙。
    # 空文字は「自業種から答えた」と「そもそも軸を評価していない」の両方を取る。
    # 素性は対応する `smg_*` が非 null の行でだけ意味を持つ。
    smg_market_fallback: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelDiagnostics:
    asof: str
    # panel 構築に使った screening rules の内容 hash。evaluate は全 cohort の
    # 一致を検証し、rules 改訂後の再構築漏れで新旧 rank が同一評価に混在する
    # 操作ミスを機械的に検出する。
    rules_hash: str
    universe_size: int
    population_size: int
    # 保存済みdiagnosticsのwire名。旧Stock Analysis契約で再構成した件数。
    security_analyses: int
    nominated_candidates: int
    bars_tickers_not_in_master: int
    effective_bars_start: str
    effective_fin_start: str
    bars_window_clamped: bool
    fin_window_clamped: bool
    population_per_trailing_nonnull: int
    population_pbr_nonnull: int
    population_ocf_yield_nonnull: int
    # EDINET 由来の軸 (ev_ebitda / net_cash / fcf_yield / asset_backed_ratio) を
    # 持つ母集団の行数。古い cohort は source coverageの外で 0 になり得て、
    # productionと同じ入力面を再現していない。単独のblockerにはせず、
    # 判定は読み手が件数から導く。
    population_edinet_axis_nonnull: int = 0
    population_per_trailing_exact: int
    # Candidate Discoveryがproductionと同じJPX除外を再現できたか。flagged rowが
    # 0件でも全required sourceが観測済みならcompleteであり、snapshot不在とは分ける。
    candidate_discovery_jpx_input_status: CandidateDiscoveryInputStatus = "unavailable"
    master_snapshot_date: str | None = None
    master_snapshot_status: str = "unavailable"
    master_population_count: int = 0
    candidate_population_count: int = 0
    policy_exclusion_reason_counts: dict[str, int] | None = None
    # Survivorship is a property of the population, not of a single forward
    # observation, so it is measured here. The bar store keeps rows for every
    # ticker that traded, including ones that have since left the market, so the
    # set priced on ``asof`` is observable independently of the master snapshot
    # and can be compared against it. This diagnostic deliberately counts only names
    # priced on the ``asof`` session. An earlier last trade does not prove delisting:
    # suspension, illiquidity and missing data can also explain it. This count alone
    # therefore does not establish coverage of every listed security.
    #
    # ``asof_population_mismatch_count`` counts tickers priced on ``asof`` that
    # the master read does not contain. A later master misses names that were
    # listed then and have since delisted (the survivorship hole); an earlier one
    # misses names listed after it. Either way the panel cross-section is not the
    # investable universe of ``asof``. Zero is expected for aligned exact-date sources,
    # but does not prove source completeness or explain every mismatch.
    #
    # ``priced_master_without_universe_count`` counts the opposite direction: a
    # name priced on ``asof`` and present in the master that the panel still
    # could not evaluate. Those rows exist in the panel without metrics, so the
    # count keeps them from reading as names that were simply absent.
    #
    # The verdict is derived from these counts by the reader, not frozen here, so
    # that changing what counts as complete reaches cohorts already on disk.
    asof_priced_count: int = 0
    asof_population_mismatch_count: int = 0
    policy_excluded_priced_count: int = 0
    priced_master_without_universe_count: int = 0
    # The lag the forward entry resolution was built with. A cohort written under
    # a different rule carries different observations for the same inputs, so the
    # reader needs to see which rule produced it.
    entry_resolution_lag_days: int = 0
    panel_variant: PanelVariant = "production"
    production_authority: bool = True
    self_range_history_sessions: int = VALUATION_HISTORY_SESSIONS
    bars_input_window_days: int = BARS_INPUT_WINDOW_DAYS


@dataclass(frozen=True, slots=True)
class PanelBuildResult:
    rows: tuple[PanelRow, ...]
    diagnostics: PanelDiagnostics


def build_panel(
    asof_date: date,
    *,
    sqlite_path: Path,
    rules: ScreeningRules,
    policy: PanelBuildPolicy = PRODUCTION_PANEL_POLICY,
) -> PanelBuildResult:
    master_read = read_eq_master_asof(sqlite_path, asof_date)
    securities = list(master_read.masters)
    if not securities:
        return _unavailable_master_panel(asof_date, rules, master_read.status, policy=policy)
    bars_floor, fin_floor = _coverage_floors(sqlite_path, asof_date)
    bars_start = max(bars_floor, asof_date - timedelta(days=policy.bars_input_window_days))
    fin_start = max(fin_floor, asof_date - timedelta(days=FIN_INPUT_WINDOW_DAYS))
    return_history_start = max(
        fin_floor,
        asof_date - timedelta(days=SHAREHOLDER_RETURN_HISTORY_WINDOW_DAYS),
    )
    normalized_profit_start = max(
        fin_floor,
        asof_date - timedelta(days=NORMALIZED_EPS_HISTORY_WINDOW_DAYS),
    )
    history_start = min(return_history_start, normalized_profit_start)
    bars_read_start = min(bars_start, return_history_start)
    history_bars = read_daily_bars(sqlite_path, bars_read_start, asof_date)
    if history_bars is None:
        raise CalibrationError(
            f"daily bars are not covered for {bars_read_start.isoformat()}..{asof_date.isoformat()}"
        )
    bars = [bar for bar in history_bars if bar.traded_at >= bars_start]
    normalized_profit_split_bars = read_adjustment_factor_bars(
        sqlite_path, normalized_profit_start, asof_date
    )
    if normalized_profit_split_bars is None:
        raise CalibrationError(
            "daily bars are not covered for normalized EPS split events "
            f"{normalized_profit_start.isoformat()}..{asof_date.isoformat()}"
        )
    history_summaries = read_fin_summaries(sqlite_path, history_start, asof_date)
    if history_summaries is None:
        raise CalibrationError(
            "fin summaries are not covered for "
            f"{history_start.isoformat()}..{asof_date.isoformat()}"
        )
    summaries = [summary for summary in history_summaries if summary.disclosed_at >= fin_start]
    edinet_by_ticker = read_edinet_metrics(sqlite_path, asof_date) or {}

    bars_by_ticker = group_bars_by_ticker(bars)
    summaries_by_ticker = group_summaries_by_ticker(summaries)
    normalized_profit_split_bars_by_ticker = group_adjustment_events_by_ticker(
        normalized_profit_split_bars
    )
    history_summaries_by_ticker = group_summaries_by_ticker(history_summaries)
    shares_by_ticker = build_shares_outstanding_index(
        summaries_by_ticker,
        bars_by_ticker,
        asof_date,
        adjustment_events_by_ticker=normalized_profit_split_bars_by_ticker,
    )
    jpx_snapshot = read_jpx_regulations(sqlite_path, asof_date)
    required_jpx_sources = frozenset(rules.universe.required_jpx_flags)
    jpx_flags_by_ticker: Mapping[str, tuple[str, ...]]
    if jpx_snapshot is None:
        jpx_input_status: CandidateDiscoveryInputStatus = "unavailable"
        jpx_flags_by_ticker = {}
    else:
        observed_jpx_sources = frozenset(jpx_snapshot.source_names)
        jpx_input_status = (
            "complete" if required_jpx_sources <= observed_jpx_sources else "incomplete"
        )
        jpx_flags_by_ticker = jpx_snapshot.flags_by_ticker
    universe_result = build_universe(
        asof_date=asof_date,
        securities=securities,
        bars_by_ticker=bars_by_ticker,
        shares_outstanding_by_ticker=shares_by_ticker,
        jpx_flags_by_ticker=jpx_flags_by_ticker,
        adjustment_events_by_ticker=normalized_profit_split_bars_by_ticker,
    )
    securities_by_ticker = {
        security.ticker: security
        for security in securities
        if security.is_common_stock and security.ticker in universe_result.snapshots
    }
    median_population = candidate_comparison_population(universe_result.snapshots, rules)
    margin_latest, margin_prior_26w = read_margin_supply_demand_inputs(sqlite_path, asof_date)
    metric_result = build_metrics(
        asof_date=asof_date,
        securities_by_ticker=securities_by_ticker,
        bars_by_ticker=bars_by_ticker,
        summaries_by_ticker=summaries_by_ticker,
        edinet_by_ticker=edinet_by_ticker,
        rules=rules,
        median_population=median_population,
        margin_latest=margin_latest,
        margin_prior_26w=margin_prior_26w,
        valuation_history_sessions=policy.valuation_history_sessions,
        adjustment_events_by_ticker=normalized_profit_split_bars_by_ticker,
    )

    normalized_profit_by_ticker = {
        ticker: build_normalized_profit_signals(
            history_summaries_by_ticker.get(ticker, ()),
            normalized_profit_split_bars_by_ticker.get(ticker, ()),
            asof_date,
            close=metric_result.financials[ticker].market_price_yen,
            ttm_rules=rules.ttm,
        )
        for ticker in universe_result.snapshots
    }
    candidates: list[StockAnalysis] = []
    for ticker in sorted(universe_result.snapshots):
        candidates.append(
            build_stock_analysis(
                ticker=ticker,
                security=securities_by_ticker[ticker],
                financial=metric_result.financials[ticker],
                derived=metric_result.derived[ticker],
                universe_snapshot=universe_result.snapshots[ticker],
                normalized_per_3fy=normalized_profit_by_ticker[ticker].normalized_per_3fy,
            )
        )

    analysis_payloads = [stock_analysis_payload(candidate) for candidate in candidates]
    discovery = build_candidate_contexts(
        analysis_payloads,
        rules=rules.candidate_discovery,
        required_jpx_flags=rules.universe.required_jpx_flags,
    )

    rows: list[PanelRow] = []
    for ticker in sorted(universe_result.snapshots):
        financial = metric_result.financials[ticker]
        derived = metric_result.derived[ticker]
        snapshot = universe_result.snapshots[ticker]
        estimate = estimate_expected_return(expected_return_inputs(financial, derived))
        return_change = build_shareholder_return_change_signals(
            history_summaries_by_ticker.get(ticker, ()),
            normalized_profit_split_bars_by_ticker.get(ticker, ()),
            asof_date,
        )
        normalized_profit = normalized_profit_by_ticker[ticker]
        profitability = build_profitability_level_signals(
            summaries_by_ticker.get(ticker, ()), asof_date, rules.ttm
        )
        rows.append(
            PanelRow(
                asof=asof_date.isoformat(),
                ticker=ticker,
                sector_33=securities_by_ticker[ticker].sector_33,
                in_population=ticker in median_population,
                market_cap_oku=(
                    float(snapshot.market_cap_oku) if snapshot.market_cap_oku is not None else None
                ),
                avg_turnover_oku=snapshot.avg_turnover_oku,
                price_history_span_days=snapshot.price_history_span_days,
                close=financial.market_price_yen,
                per_forward=financial.per_forward,
                per_trailing=financial.per_trailing,
                pbr=financial.pbr,
                ev_ebitda=financial.ev_ebitda,
                p_s=financial.p_s,
                pcfr=financial.pcfr,
                ocf_yield=financial.ocf_yield,
                fcf_yield=financial.fcf_yield,
                net_cash_to_market_cap=financial.net_cash_to_market_cap,
                cash_to_market_cap=financial.cash_to_market_cap,
                investment_securities=financial.investment_securities,
                asset_backed_ratio=financial.asset_backed_ratio,
                equity_ratio=financial.equity_ratio,
                dividend_yield=financial.dividend_yield,
                eps_yoy=financial.eps_yoy,
                sales_yoy=financial.sales_yoy,
                operating_profit_yoy=financial.operating_profit_yoy,
                cfo_yoy=financial.cfo_yoy,
                accruals_to_assets=financial.accruals_to_assets,
                net_share_change_yoy=financial.net_share_change_yoy,
                tradable_share_change_yoy=financial.tradable_share_change_yoy,
                ttm_quality_per_trailing=financial.ttm_quality_per_trailing.value,
                ttm_quality_ocf_yield=financial.ttm_quality_ocf_yield.value,
                price_change_60d=derived.price_change_60d,
                gap_from_52w_low=derived.gap_from_52w_low,
                price_history_coverage_750d=derived.price_history_coverage_750d,
                smg_per_forward=derived.sector_median_gap.get("per_forward"),
                smg_per_trailing=derived.sector_median_gap.get("per_trailing"),
                smg_pbr=derived.sector_median_gap.get("pbr"),
                smg_ev_ebitda=derived.sector_median_gap.get("ev_ebitda"),
                smg_p_s=derived.sector_median_gap.get("p_s"),
                srp_per_forward=derived.self_range_position.get("per_forward"),
                srp_per_trailing=derived.self_range_position.get("per_trailing"),
                srp_pbr=derived.self_range_position.get("pbr"),
                srp_ev_ebitda=derived.self_range_position.get("ev_ebitda"),
                srp_p_s=derived.self_range_position.get("p_s"),
                er_annual=estimate.er_annual if estimate else None,
                er_reversion_annual=estimate.reversion_annual if estimate else None,
                er_carry_annual=estimate.carry_annual if estimate else None,
                er_upside_capped=estimate.upside_capped if estimate else None,
                margin_balance_date=(
                    derived.margin_balance_date.isoformat() if derived.margin_balance_date else None
                ),
                margin_long_to_adv=derived.margin_long_to_adv,
                margin_short_to_adv=derived.margin_short_to_adv,
                margin_long_share=derived.margin_long_share,
                margin_long_delta_26w=derived.margin_long_delta_26w,
                margin_std_long_share=derived.margin_std_long_share,
                realized_volatility_60d=derived.realized_volatility_60d,
                normalized_per_3fy=normalized_profit.normalized_per_3fy,
                normalized_per_5fy=normalized_profit.normalized_per_5fy,
                self_range_observed_sessions=sum(
                    bar.traded_at <= asof_date for bar in bars_by_ticker.get(ticker, ())
                ),
                operating_profit_to_assets=profitability.operating_profit_to_assets,
                operating_margin=profitability.operating_margin,
                asset_turnover=profitability.asset_turnover,
                in_nomination_union=bool(discovery.contexts[ticker].reasons),
                valuation_approaches="|".join(
                    item.valuation_approach_id for item in discovery.contexts[ticker].reasons
                ),
                valuation_approach_ranks="|".join(
                    f"{item.valuation_approach_id}:{item.rank}"
                    for item in discovery.contexts[ticker].reasons
                ),
                smg_market_fallback="|".join(
                    metric
                    for metric in VALUATION_METRICS
                    if derived.sector_median_basis.get(metric) == SECTOR_MEDIAN_BASIS_MARKET
                ),
                self_range_degraded=not policy.production_authority,
                dps_streak_up=return_change.dps_streak_up,
                dps_yoy_latest=return_change.dps_yoy_latest,
                dps_guidance_up=return_change.dps_guidance_up,
                dividend_initiation=return_change.dividend_initiation,
                share_count_reduction_streak=(return_change.share_count_reduction_streak),
                shareholder_return_change=return_change.shareholder_return_change,
            )
        )

    asof_priced = {
        ticker
        for ticker, ticker_bars in bars_by_ticker.items()
        if ticker_bars and ticker_bars[-1].traded_at == asof_date
    }
    # A historical master member without enough local bars is unavailable data,
    # not a silently excluded survivor. Keep an explicit row so its forward
    # observation and cohort coverage remain visible to authority checks. A name the
    # policy removed is the other kind and belongs in the counts below instead.
    #
    # Both sides read the reasons `build_universe` already decided. Re-deriving them
    # here is what let the same diagnostic name mean two different quantities: the
    # policy conditions live in one place, so a condition added there reaches both
    # surfaces without anyone remembering to copy it.
    policy_reasons = frozenset(POLICY_EXCLUSION_REASONS)
    for security in securities:
        if security.ticker in universe_result.snapshots:
            continue
        flags = frozenset(universe_result.exclusion_flags.get(security.ticker, ()))
        if flags & policy_reasons:
            continue
        rows.append(
            _unresolved_master_member_row(
                asof_date,
                security.ticker,
                security.sector_33,
                priced_at_asof=security.ticker in asof_priced,
                self_range_degraded=not policy.production_authority,
            )
        )

    policy_exclusions = {
        reason: count
        for reason, count in universe_result.exclusion_counts.items()
        if reason in policy_reasons
    }
    population_rows = [row for row in rows if row.in_population]
    panel_tickers = {row.ticker for row in rows}
    master_tickers = {security.ticker for security in securities}
    population_mismatch = asof_priced - master_tickers
    diagnostics = PanelDiagnostics(
        asof=asof_date.isoformat(),
        rules_hash=rules_content_hash(rules, policy),
        universe_size=len(universe_result.snapshots),
        population_size=len(median_population),
        security_analyses=len(candidates),
        nominated_candidates=discovery.candidate_count,
        bars_tickers_not_in_master=sum(
            1 for ticker in bars_by_ticker if ticker not in master_tickers
        ),
        effective_bars_start=bars_start.isoformat(),
        effective_fin_start=fin_start.isoformat(),
        bars_window_clamped=bars_start > asof_date - timedelta(days=policy.bars_input_window_days),
        fin_window_clamped=fin_start > asof_date - timedelta(days=FIN_INPUT_WINDOW_DAYS),
        population_per_trailing_nonnull=sum(
            1 for row in population_rows if row.per_trailing is not None
        ),
        population_pbr_nonnull=sum(1 for row in population_rows if row.pbr is not None),
        population_ocf_yield_nonnull=sum(1 for row in population_rows if row.ocf_yield is not None),
        population_edinet_axis_nonnull=sum(
            1
            for row in population_rows
            if row.ev_ebitda is not None
            or row.net_cash_to_market_cap is not None
            or row.fcf_yield is not None
            or row.asset_backed_ratio is not None
        ),
        population_per_trailing_exact=sum(
            1 for row in population_rows if row.ttm_quality_per_trailing == TTMQuality.EXACT.value
        ),
        candidate_discovery_jpx_input_status=jpx_input_status,
        master_snapshot_date=(
            master_read.snapshot_date.isoformat() if master_read.snapshot_date is not None else None
        ),
        master_snapshot_status=master_read.status,
        master_population_count=len(securities),
        candidate_population_count=len(rows),
        policy_exclusion_reason_counts=policy_exclusions,
        asof_priced_count=len(asof_priced),
        asof_population_mismatch_count=len(population_mismatch),
        policy_excluded_priced_count=len((asof_priced & master_tickers) - panel_tickers),
        priced_master_without_universe_count=len(
            (asof_priced & master_tickers & panel_tickers) - set(universe_result.snapshots)
        ),
        entry_resolution_lag_days=STALE_PRICE_MAX_LAG_DAYS,
        panel_variant=policy.variant,
        production_authority=policy.production_authority,
        self_range_history_sessions=policy.valuation_history_sessions,
        bars_input_window_days=policy.bars_input_window_days,
    )
    return PanelBuildResult(rows=tuple(rows), diagnostics=diagnostics)


def _unresolved_master_member_row(
    asof_date: date,
    ticker: str,
    sector_33: str,
    *,
    priced_at_asof: bool,
    self_range_degraded: bool,
) -> PanelRow:
    return PanelRow(
        asof=asof_date.isoformat(),
        ticker=ticker,
        sector_33=sector_33,
        in_population=True,
        market_cap_oku=None,
        avg_turnover_oku=None,
        price_history_span_days=None,
        close=None,
        per_forward=None,
        per_trailing=None,
        pbr=None,
        ev_ebitda=None,
        p_s=None,
        pcfr=None,
        ocf_yield=None,
        fcf_yield=None,
        net_cash_to_market_cap=None,
        cash_to_market_cap=None,
        investment_securities=None,
        asset_backed_ratio=None,
        equity_ratio=None,
        dividend_yield=None,
        eps_yoy=None,
        sales_yoy=None,
        operating_profit_yoy=None,
        cfo_yoy=None,
        accruals_to_assets=None,
        net_share_change_yoy=None,
        tradable_share_change_yoy=None,
        ttm_quality_per_trailing="unavailable",
        ttm_quality_ocf_yield="unavailable",
        price_change_60d=None,
        gap_from_52w_low=None,
        price_history_coverage_750d=None,
        smg_per_forward=None,
        smg_per_trailing=None,
        smg_pbr=None,
        smg_ev_ebitda=None,
        smg_p_s=None,
        srp_per_forward=None,
        srp_per_trailing=None,
        srp_pbr=None,
        srp_ev_ebitda=None,
        srp_p_s=None,
        er_annual=None,
        er_reversion_annual=None,
        er_carry_annual=None,
        er_upside_capped=None,
        margin_balance_date=None,
        margin_long_to_adv=None,
        margin_short_to_adv=None,
        margin_long_share=None,
        margin_long_delta_26w=None,
        margin_std_long_share=None,
        realized_volatility_60d=None,
        in_nomination_union=False,
        valuation_approaches="",
        valuation_approach_ranks="",
        population_coverage_status=(
            "priced_master_without_universe"
            if priced_at_asof
            else "master_without_universe_unpriced"
        ),
        self_range_degraded=self_range_degraded,
    )


def _unavailable_master_panel(
    asof_date: date,
    rules: ScreeningRules,
    master_status: str,
    *,
    policy: PanelBuildPolicy,
) -> PanelBuildResult:
    """Persist an unresolved cohort instead of silently removing it from evaluation."""
    diagnostics = PanelDiagnostics(
        asof=asof_date.isoformat(),
        rules_hash=rules_content_hash(rules, policy),
        universe_size=0,
        population_size=0,
        security_analyses=0,
        nominated_candidates=0,
        bars_tickers_not_in_master=0,
        effective_bars_start=asof_date.isoformat(),
        effective_fin_start=asof_date.isoformat(),
        bars_window_clamped=False,
        fin_window_clamped=False,
        population_per_trailing_nonnull=0,
        population_pbr_nonnull=0,
        population_ocf_yield_nonnull=0,
        population_per_trailing_exact=0,
        master_snapshot_date=None,
        master_snapshot_status=master_status,
        master_population_count=0,
        candidate_population_count=0,
        policy_exclusion_reason_counts={},
        panel_variant=policy.variant,
        production_authority=policy.production_authority,
        self_range_history_sessions=policy.valuation_history_sessions,
        bars_input_window_days=policy.bars_input_window_days,
    )
    return PanelBuildResult(rows=(), diagnostics=diagnostics)


def _coverage_floors(sqlite_path: Path, asof_date: date) -> tuple[date, date]:
    """The earliest dates the history windows may start from.

    The floor is where the store can be **read** from, which is the oldest row only
    while the store may still serve it. Summaries come from a moving subscription
    window, so rows fetched years ago outlive the window that produced them; taking
    the floor from the oldest row alone asks `read_fin_summaries` for a range it
    refuses, and every cohort fails over filings at the far edge of the history.
    A raised floor shortens the history windows exactly as a younger store does, and
    the rows it leaves behind stay unread rather than entering a cohort through a
    window the store no longer stands behind.
    """
    conn = sqlite3.connect(f"{sqlite_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        bars_min = conn.execute("SELECT MIN(traded_at) FROM jquants_daily_bars").fetchone()[0]
        fin_min = conn.execute("SELECT MIN(disclosed_at) FROM jquants_fin_summaries").fetchone()[0]
    finally:
        conn.close()
    if bars_min is None or fin_min is None:
        raise CalibrationError("SQLite cache has no bars or fin summaries")
    fin_floor = date.fromisoformat(str(fin_min))
    readable_from = fin_summaries_readable_from(sqlite_path, asof_date)
    if readable_from is not None:
        fin_floor = max(fin_floor, readable_from)
    return date.fromisoformat(str(bars_min)), fin_floor


PANEL_FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in fields(PanelRow))
DIAGNOSTIC_FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in fields(PanelDiagnostics))
