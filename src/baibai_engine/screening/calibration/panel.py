"""Point-in-time panel: 過去 asof で全銘柄の指標・screen 判定・select 順位を再構成する。

本番 run と同じ部品 (`build_metrics` / `evaluate_screening` / `candidate_entry` /
`build_selection_payload`) をそのまま呼ぶことで、リプレイと本番のロジック一致を
実装の単一性で担保する。相違点は入力の中立化だけ:

- macro_context=None (macro は annotation であり順位に使わない)
- previous_candidates=None (現在の候補履歴を過去 cohort の順位へ混入させない)
- JPX 規制 flag は過去断面が cache に無いため空 (除外は annotation 数銘柄規模)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Literal

from baibai_engine.foundation.coerce import int_or, string_or_none
from baibai_engine.market.store import read_daily_bars

from ..candidate_build import build_screened_candidate
from ..estimates import estimate_expected_return
from ..metrics import (
    BARS_INPUT_WINDOW_DAYS,
    FIN_INPUT_WINDOW_DAYS,
    build_metrics,
    build_shares_outstanding_index,
    group_bars_by_ticker,
    group_summaries_by_ticker,
)
from ..render import candidate_entry
from ..rule_config import ScreeningRules
from ..rules import evaluate_screening
from ..schema import ScreenedCandidate, TTMQuality
from ..selection import build_selection_payload
from ..selection.records import candidate_record_from_mapping
from ..sqlite_reader import read_edinet_metrics, read_eq_master_asof, read_fin_summaries
from ..universe import ELIGIBLE_MARKETS, build_universe, liquid_median_population
from .forward import STALE_PRICE_MAX_LAG_DAYS

# select リプレイで記録する production-diversity 推奨順位の深さ。
RECOMMENDED_RANK_DEPTH = 50


class CalibrationError(RuntimeError):
    """Raised when the cache cannot serve a point-in-time panel build."""


def rules_content_hash(rules: ScreeningRules) -> str:
    """screening rules の内容 hash (semantic identity)。panel provenance に使う。"""
    return sha256(rules.model_dump_json().encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelRow:
    asof: str
    ticker: str
    sector_33: str
    in_population: bool
    market_cap_oku: float | None
    avg_turnover_oku: float | None
    listing_span_days: int | None
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
    equity_ratio: float | None
    price_to_equity: float | None
    dividend_yield: float | None
    eps_yoy: float | None
    sales_yoy: float | None
    operating_profit_yoy: float | None
    cfo_yoy: float | None
    accruals_to_assets: float | None
    net_share_change_yoy: float | None
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
    pass_screen: bool
    evidence_playbooks: str
    selection_rank: int | None
    recommended_rank: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelDiagnostics:
    asof: str
    # panel 構築に使った screening rules の内容 hash。evaluate は全 cohort の
    # 一致を検証し、rules 改訂後の再構築漏れで新旧 rank が同一評価に混在する
    # 操作ミスを機械的に検出する。
    rules_hash: str
    universe_size: int
    population_size: int
    candidates: int
    evidence_candidates: int
    bars_tickers_not_in_master: int
    effective_bars_start: str
    effective_fin_start: str
    bars_window_clamped: bool
    fin_window_clamped: bool
    population_per_trailing_nonnull: int
    population_pbr_nonnull: int
    population_ocf_yield_nonnull: int
    population_per_trailing_exact: int
    master_snapshot_date: str | None = None
    master_snapshot_status: str = "unavailable"
    master_population_count: int = 0
    candidate_population_count: int = 0
    policy_exclusion_reason_counts: dict[str, int] | None = None
    # Survivorship is a property of the population, not of a single forward
    # observation, so it is measured here. The bar store keeps rows for every
    # ticker that traded, including ones that have since left the market, so the
    # at-asof priced set is observable independently of the master snapshot and
    # can be compared against it.
    #
    # ``asof_population_mismatch_count`` counts tickers priced at ``asof`` that
    # the master read does not contain. A later master misses names that were
    # listed then and have since delisted (the survivorship hole); an earlier one
    # misses names listed after it. Either way the panel cross-section is not the
    # investable universe of ``asof``, so both count as incomplete coverage. An
    # exact-date master drives the count to zero by construction.
    asof_priced_count: int = 0
    asof_population_mismatch_count: int = 0
    policy_excluded_priced_count: int = 0
    survivorship_coverage_status: str = "unavailable"


@dataclass(frozen=True, slots=True)
class PanelBuildResult:
    rows: tuple[PanelRow, ...]
    diagnostics: PanelDiagnostics


def build_panel(
    asof_date: date,
    *,
    sqlite_path: Path,
    rules: ScreeningRules,
) -> PanelBuildResult:
    master_read = read_eq_master_asof(sqlite_path, asof_date)
    securities = list(master_read.masters)
    if not securities:
        return _unavailable_master_panel(asof_date, rules, master_read.status)
    bars_floor, fin_floor = _coverage_floors(sqlite_path)
    bars_start = max(bars_floor, asof_date - timedelta(days=BARS_INPUT_WINDOW_DAYS))
    fin_start = max(fin_floor, asof_date - timedelta(days=FIN_INPUT_WINDOW_DAYS))
    bars = read_daily_bars(sqlite_path, bars_start, asof_date)
    if bars is None:
        raise CalibrationError(
            f"daily bars are not covered for {bars_start.isoformat()}..{asof_date.isoformat()}"
        )
    summaries = read_fin_summaries(sqlite_path, fin_start, asof_date)
    if summaries is None:
        raise CalibrationError(
            f"fin summaries are not covered for {fin_start.isoformat()}..{asof_date.isoformat()}"
        )
    edinet_by_ticker = read_edinet_metrics(sqlite_path, asof_date) or {}

    bars_by_ticker = group_bars_by_ticker(bars)
    summaries_by_ticker = group_summaries_by_ticker(summaries)
    shares_by_ticker = build_shares_outstanding_index(
        summaries_by_ticker, bars_by_ticker, asof_date
    )
    universe_result = build_universe(
        asof_date=asof_date,
        securities=securities,
        bars_by_ticker=bars_by_ticker,
        shares_outstanding_by_ticker=shares_by_ticker,
        jpx_flags_by_ticker={},
    )
    securities_by_ticker = {
        security.code: security
        for security in securities
        if security.is_common_stock and security.code in universe_result.snapshots
    }
    median_population = liquid_median_population(universe_result.snapshots, rules)
    metric_result = build_metrics(
        asof_date=asof_date,
        securities_by_ticker=securities_by_ticker,
        bars_by_ticker=bars_by_ticker,
        summaries_by_ticker=summaries_by_ticker,
        edinet_by_ticker=edinet_by_ticker,
        rules=rules,
        median_population=median_population,
    )

    evidence_by_ticker: dict[str, tuple[str, ...]] = {}
    candidates: list[ScreenedCandidate] = []
    for ticker in sorted(universe_result.snapshots):
        result = evaluate_screening(
            metric_result.financials[ticker],
            metric_result.derived[ticker],
            rules,
            sector_33=securities_by_ticker[ticker].sector_33,
        )

        if result.pass_fail:
            evidence_by_ticker[ticker] = tuple(hit.name for hit in result.evidence_hits)
        candidates.append(
            build_screened_candidate(
                ticker=ticker,
                security=securities_by_ticker[ticker],
                financial=metric_result.financials[ticker],
                derived=metric_result.derived[ticker],
                universe_snapshot=universe_result.snapshots[ticker],
                evidence_hits=result.evidence_hits if result.pass_fail else (),
            )
        )

    selection_rank = _replay_ranks(
        asof_date, candidates, rules, mode="full_ranking", depth=len(candidates)
    )
    recommended_rank = _replay_ranks(
        asof_date, candidates, rules, mode="production_diversity", depth=RECOMMENDED_RANK_DEPTH
    )

    latest_close_by_ticker: dict[str, float] = {}
    for ticker, ticker_bars in bars_by_ticker.items():
        for bar in reversed(ticker_bars):
            if bar.traded_at <= asof_date:
                latest_close_by_ticker[ticker] = bar.close
                break

    rows: list[PanelRow] = []
    for ticker in sorted(universe_result.snapshots):
        financial = metric_result.financials[ticker]
        derived = metric_result.derived[ticker]
        snapshot = universe_result.snapshots[ticker]
        estimate = estimate_expected_return(
            financial, derived, close=latest_close_by_ticker.get(ticker)
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
                listing_span_days=snapshot.listing_span_days,
                close=latest_close_by_ticker.get(ticker),
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
                equity_ratio=financial.equity_ratio,
                price_to_equity=financial.price_to_equity,
                dividend_yield=financial.dividend_yield,
                eps_yoy=financial.eps_yoy,
                sales_yoy=financial.sales_yoy,
                operating_profit_yoy=financial.operating_profit_yoy,
                cfo_yoy=financial.cfo_yoy,
                accruals_to_assets=financial.accruals_to_assets,
                net_share_change_yoy=financial.net_share_change_yoy,
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
                srp_per_forward=derived.self_range_percentile.get("per_forward"),
                srp_per_trailing=derived.self_range_percentile.get("per_trailing"),
                srp_pbr=derived.self_range_percentile.get("pbr"),
                srp_ev_ebitda=derived.self_range_percentile.get("ev_ebitda"),
                srp_p_s=derived.self_range_percentile.get("p_s"),
                er_annual=estimate.er_annual if estimate else None,
                er_reversion_annual=estimate.reversion_annual if estimate else None,
                er_carry_annual=estimate.carry_annual if estimate else None,
                er_upside_capped=estimate.upside_capped if estimate else None,
                pass_screen=ticker in evidence_by_ticker,
                evidence_playbooks="|".join(evidence_by_ticker.get(ticker, ())),
                selection_rank=selection_rank.get(ticker),
                recommended_rank=recommended_rank.get(ticker),
            )
        )

    # A historical master member without enough local bars is unavailable data,
    # not a silently excluded survivor. Keep an explicit row so its forward
    # observation and cohort coverage remain visible to authority checks.
    for security in securities:
        if (
            security.code not in universe_result.snapshots
            and security.is_common_stock
            and security.market_segment.upper() in ELIGIBLE_MARKETS
        ):
            rows.append(_unresolved_master_member_row(asof_date, security.code, security.sector_33))

    policy_exclusions: dict[str, int] = {}
    for security in securities:
        if not security.is_common_stock:
            policy_exclusions["non_common_stock"] = policy_exclusions.get("non_common_stock", 0) + 1
        elif security.market_segment.upper() not in ELIGIBLE_MARKETS:
            policy_exclusions["market_out_of_scope"] = (
                policy_exclusions.get("market_out_of_scope", 0) + 1
            )
    population_rows = [row for row in rows if row.in_population]
    panel_tickers = {row.ticker for row in rows}
    master_tickers = {security.code for security in securities}
    # Bars arrive ordered by (ticker, traded_at) within a window ending at asof, so
    # the last bar of each ticker is its latest priced day at or before asof.
    entry_floor = asof_date - timedelta(days=STALE_PRICE_MAX_LAG_DAYS)
    asof_priced = {
        ticker
        for ticker, ticker_bars in bars_by_ticker.items()
        if ticker_bars and ticker_bars[-1].traded_at >= entry_floor
    }
    population_mismatch = asof_priced - master_tickers
    diagnostics = PanelDiagnostics(
        asof=asof_date.isoformat(),
        rules_hash=rules_content_hash(rules),
        universe_size=len(universe_result.snapshots),
        population_size=len(median_population),
        candidates=len(candidates),
        evidence_candidates=len(evidence_by_ticker),
        bars_tickers_not_in_master=sum(
            1 for ticker in bars_by_ticker if ticker not in securities_by_ticker
        ),
        effective_bars_start=bars_start.isoformat(),
        effective_fin_start=fin_start.isoformat(),
        bars_window_clamped=bars_start > asof_date - timedelta(days=BARS_INPUT_WINDOW_DAYS),
        fin_window_clamped=fin_start > asof_date - timedelta(days=FIN_INPUT_WINDOW_DAYS),
        population_per_trailing_nonnull=sum(
            1 for row in population_rows if row.per_trailing is not None
        ),
        population_pbr_nonnull=sum(1 for row in population_rows if row.pbr is not None),
        population_ocf_yield_nonnull=sum(1 for row in population_rows if row.ocf_yield is not None),
        population_per_trailing_exact=sum(
            1 for row in population_rows if row.ttm_quality_per_trailing == TTMQuality.EXACT.value
        ),
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
        survivorship_coverage_status=("complete" if not population_mismatch else "incomplete"),
    )
    return PanelBuildResult(rows=tuple(rows), diagnostics=diagnostics)


def _unresolved_master_member_row(asof_date: date, ticker: str, sector_33: str) -> PanelRow:
    return PanelRow(
        asof=asof_date.isoformat(),
        ticker=ticker,
        sector_33=sector_33,
        in_population=True,
        market_cap_oku=None,
        avg_turnover_oku=None,
        listing_span_days=None,
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
        equity_ratio=None,
        price_to_equity=None,
        dividend_yield=None,
        eps_yoy=None,
        sales_yoy=None,
        operating_profit_yoy=None,
        cfo_yoy=None,
        accruals_to_assets=None,
        net_share_change_yoy=None,
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
        pass_screen=False,
        evidence_playbooks="",
        selection_rank=None,
        recommended_rank=None,
    )


def _unavailable_master_panel(
    asof_date: date, rules: ScreeningRules, master_status: str
) -> PanelBuildResult:
    """Persist an unresolved cohort instead of silently removing it from evaluation."""
    diagnostics = PanelDiagnostics(
        asof=asof_date.isoformat(),
        rules_hash=rules_content_hash(rules),
        universe_size=0,
        population_size=0,
        candidates=0,
        evidence_candidates=0,
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
    )
    return PanelBuildResult(rows=(), diagnostics=diagnostics)


def _replay_ranks(
    asof_date: date,
    candidates: list[ScreenedCandidate],
    rules: ScreeningRules,
    *,
    mode: Literal["full_ranking", "production_diversity"],
    depth: int,
) -> dict[str, int]:
    """Replay the production selection and return ticker -> 1-based rank.

    ``full_ranking`` は E[r] 降順の全順位、``production_diversity`` は本番 depth
    内の推奨順位。どちらも本番の `build_selection_payload` を通す (順位ロジックの
    複製をしない) 。
    """
    if not candidates:
        return {}
    records = [
        candidate_record_from_mapping(candidate_entry(candidate)) for candidate in candidates
    ]
    profile = rules.selection.default_profile
    replay_rules = _rules_with_uncapped_target(rules)
    profile_overrides: dict[str, dict[str, object]] | None = None
    if mode == "full_ranking":
        profile_overrides = {
            profile: {
                "diversity": {
                    "max_recommended_per_sector": 10**9,
                    "max_recommended_per_playbook": 10**9,
                    "max_previous_candidates_in_recommended": None,
                }
            }
        }
    payload = build_selection_payload(
        asof_date=asof_date,
        candidates=records,
        macro_context=None,
        rules=replay_rules,
        top=max(depth, 1),
        profile=profile,
        candidates_ref="calibration-replay",
        macro_context_ref=None,
        previous_candidates=None,
        market_regime=None,
        profile_overrides=profile_overrides,
        detail="summary",
    )
    ranks: dict[str, int] = {}
    recommendations = payload.get("recommendations")
    if isinstance(recommendations, list):
        for item in recommendations:
            if not isinstance(item, dict):
                continue
            ticker = string_or_none(item.get("ticker"))
            rank = int_or(item.get("rank"), 0)
            if ticker is not None and rank > 0:
                ranks[ticker] = rank
    return ranks


def _rules_with_uncapped_target(rules: ScreeningRules) -> ScreeningRules:
    """research_selection_target_max=0 (=uncapped) のコピーを返す。

    本番の推奨は 5 件で切られるが、リプレイでは top-10/20 の評価と全順位の
    記録が要るため、`_research_recommendation_limit` の cap を外して `top` で
    深さを制御する。
    """
    data = rules.model_dump(mode="python")
    output = dict(data.get("output") or {})
    output["research_selection_target_max"] = 0
    data["output"] = output
    return ScreeningRules.model_validate(data)


def _coverage_floors(sqlite_path: Path) -> tuple[date, date]:
    conn = sqlite3.connect(sqlite_path)
    try:
        bars_min = conn.execute("SELECT MIN(traded_at) FROM jquants_daily_bars").fetchone()[0]
        fin_min = conn.execute("SELECT MIN(disclosed_at) FROM jquants_fin_summaries").fetchone()[0]
    finally:
        conn.close()
    if bars_min is None or fin_min is None:
        raise CalibrationError("SQLite cache has no bars or fin summaries")
    return date.fromisoformat(str(bars_min)), date.fromisoformat(str(fin_min))


PANEL_FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in fields(PanelRow))
DIAGNOSTIC_FIELD_NAMES: tuple[str, ...] = tuple(field.name for field in fields(PanelDiagnostics))
