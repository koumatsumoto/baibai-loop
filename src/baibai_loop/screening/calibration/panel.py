"""Point-in-time panel: 過去 asof で全銘柄の指標・screen 判定・select 順位を再構成する。

本番 run と同じ部品 (`build_metrics` / `evaluate_screening` / `candidate_entry` /
`build_selection_payload`) をそのまま呼ぶことで、リプレイと本番のロジック一致を
実装の単一性で担保する。相違点は入力の中立化だけ:

- macro_context=None (macro は annotation であり順位に使わない)
- prior_research_by_ticker={} / previous_candidates=None (L3 record 由来の
  suppression は published_at ≤ asof の PIT チェックを持たず、現在の判断が過去
  cohort へ漏れるため遮断する)
- JPX 規制 flag は過去断面が cache に無いため空 (除外は annotation 数銘柄規模)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from baibai_loop.foundation.coerce import int_or, string_or_none
from baibai_loop.market.store import read_daily_bars

from ..candidate_build import build_screened_candidate
from ..metrics import (
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
from ..sqlite_reader import read_edinet_metrics, read_eq_master, read_fin_summaries
from ..universe import build_universe, liquid_median_population

# 本番 run と同じ窓 (cli/run.py)。cache の coverage 床より前はクランプする。
BARS_WINDOW_DAYS = 1200
FIN_WINDOW_DAYS = 730

# select リプレイで記録する production-diversity 推奨順位の深さ。
RECOMMENDED_RANK_DEPTH = 50


class CalibrationError(RuntimeError):
    """Raised when the cache cannot serve a point-in-time panel build."""


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
    pass_screen: bool
    evidence_playbooks: str
    selection_rank: int | None
    recommended_rank: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class PanelDiagnostics:
    asof: str
    universe_size: int
    population_size: int
    candidates: int
    bars_tickers_not_in_master: int
    effective_bars_start: str
    effective_fin_start: str
    bars_window_clamped: bool
    fin_window_clamped: bool
    population_per_trailing_nonnull: int
    population_pbr_nonnull: int
    population_ocf_yield_nonnull: int
    population_per_trailing_exact: int


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
    bars_floor, fin_floor = _coverage_floors(sqlite_path)
    bars_start = max(bars_floor, asof_date - timedelta(days=BARS_WINDOW_DAYS))
    fin_start = max(fin_floor, asof_date - timedelta(days=FIN_WINDOW_DAYS))

    securities = read_eq_master(sqlite_path)
    if securities is None:
        raise CalibrationError("eq_master is not available in the SQLite cache")
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
    screened: list[ScreenedCandidate] = []
    for ticker in sorted(universe_result.snapshots):
        result = evaluate_screening(
            metric_result.financials[ticker],
            metric_result.derived[ticker],
            rules,
            sector_33=securities_by_ticker[ticker].sector_33,
        )
        if not result.pass_fail:
            continue
        evidence_by_ticker[ticker] = tuple(hit.name for hit in result.evidence_hits)
        screened.append(
            build_screened_candidate(
                ticker=ticker,
                security=securities_by_ticker[ticker],
                financial=metric_result.financials[ticker],
                derived=metric_result.derived[ticker],
                universe_snapshot=universe_result.snapshots[ticker],
                evidence_hits=result.evidence_hits,
            )
        )

    selection_rank = _replay_ranks(
        asof_date, screened, rules, mode="full_ranking", depth=len(screened)
    )
    recommended_rank = _replay_ranks(
        asof_date, screened, rules, mode="production_diversity", depth=RECOMMENDED_RANK_DEPTH
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
                dividend_yield=None,
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
                pass_screen=ticker in evidence_by_ticker,
                evidence_playbooks="|".join(evidence_by_ticker.get(ticker, ())),
                selection_rank=selection_rank.get(ticker),
                recommended_rank=recommended_rank.get(ticker),
            )
        )

    population_rows = [row for row in rows if row.in_population]
    diagnostics = PanelDiagnostics(
        asof=asof_date.isoformat(),
        universe_size=len(universe_result.snapshots),
        population_size=len(median_population),
        candidates=len(screened),
        bars_tickers_not_in_master=sum(
            1 for ticker in bars_by_ticker if ticker not in securities_by_ticker
        ),
        effective_bars_start=bars_start.isoformat(),
        effective_fin_start=fin_start.isoformat(),
        bars_window_clamped=bars_start > asof_date - timedelta(days=BARS_WINDOW_DAYS),
        fin_window_clamped=fin_start > asof_date - timedelta(days=FIN_WINDOW_DAYS),
        population_per_trailing_nonnull=sum(
            1 for row in population_rows if row.per_trailing is not None
        ),
        population_pbr_nonnull=sum(1 for row in population_rows if row.pbr is not None),
        population_ocf_yield_nonnull=sum(1 for row in population_rows if row.ocf_yield is not None),
        population_per_trailing_exact=sum(
            1 for row in population_rows if row.ttm_quality_per_trailing == TTMQuality.EXACT.value
        ),
    )
    return PanelBuildResult(rows=tuple(rows), diagnostics=diagnostics)


def _replay_ranks(
    asof_date: date,
    screened: list[ScreenedCandidate],
    rules: ScreeningRules,
    *,
    mode: Literal["full_ranking", "production_diversity"],
    depth: int,
) -> dict[str, int]:
    """Replay the production selection and return ticker -> 1-based rank.

    ``full_ranking`` は diversity cap を実質無効化した「割安度そのままの順位」、
    ``production_diversity`` は本番の diversity cap を適用した推奨順位。どちらも
    本番の `build_selection_payload` を通す (順位ロジックの複製をしない) 。
    """
    if not screened:
        return {}
    candidates = [
        candidate_record_from_mapping(candidate_entry(candidate)) for candidate in screened
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
        candidates=candidates,
        macro_context=None,
        rules=replay_rules,
        top=max(depth, 1),
        profile=profile,
        candidates_ref="calibration-replay",
        macro_context_ref=None,
        previous_candidates=None,
        prior_research_by_ticker={},
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
