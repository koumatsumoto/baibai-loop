from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, timedelta
from itertools import pairwise
from math import isfinite, sqrt
from statistics import fmean, mean, median

from baibai_engine.market.bars import asof_basis_closes

from .margin_metrics import margin_supply_demand
from .providers.edinet import EdinetMetricRecord
from .providers.jquants import (
    JQuantsDailyBar,
    JQuantsFinancialSummary,
    JQuantsWeeklyMargin,
)
from .rule_config import ScreeningRules, TTMRules, load_screening_rules
from .schema import (
    DerivedMetrics,
    FinancialSnapshot,
    OperatingProfitSource,
    SecurityMaster,
    TTMQuality,
)

VALUATION_METRICS = ("per_forward", "per_trailing", "pbr", "ev_ebitda", "p_s")

# 自己レンジ / sigma gap が前提にする約 3 年の価格履歴窓(暦日)。listing 起点の
# short_history_flag では検出できない「上場は古いが bar 履歴に長期ギャップがある」
# 銘柄を、窓内の bar 本数(対 population 最大比)として事実記録するために使う。
PRICE_HISTORY_WINDOW_DAYS = 750

# valuation history の自己レンジに使う直近 bar 本数(立会日)。上の窓が暦日なのに対し
# こちらは session 数で、同じ 750 でも表す量が違う。
VALUATION_HISTORY_SESSIONS = 750

# metric 計算の入力窓 (暦日)。bars は 3 年自己レンジ percentile / sigma_gap に
# 1200 日、fin summaries は TTM 合成と前年同期 YoY に 730 日を要する。本番 run と
# 較正リプレイ (calibration/panel.py) が同じ値を import する。窓がずれると
# リプレイは本番と別物の指標を測るため、ここ以外に窓を定義しない。
# 26 週の建玉変化を測る窓を立会日で表した本数。株式分割はこの窓を跨ぐと株数基準が
# 変わるので、跨いだ銘柄は軸を答えない。
MARGIN_DELTA_SESSIONS = 130

BARS_INPUT_WINDOW_DAYS = 1200
FIN_INPUT_WINDOW_DAYS = 730

# 株主還元の変化は3期の通期実績を必要とするため、calibration panel だけが使う
# 補助履歴窓。production の FinancialSnapshot / E[r] 入力窓は上の730日のままにし、
# この窓を build_metrics へ渡さない。
SHAREHOLDER_RETURN_HISTORY_WINDOW_DAYS = 1200
NORMALIZED_EPS_HISTORY_WINDOW_DAYS = 2200

# 通期実績 DPS の accrual 期間を bound する暦日窓。前期の通期実績開示行が窓内に
# 無い (実績が 1 期分しか無い) ときのフォールバックで、開示日から約 1 年遡って
# その期間内・開示前の分割を carry へ反映するために使う。
DIVIDEND_ACCRUAL_LOOKBACK_DAYS = 400


@dataclass(frozen=True)
class MetricBuildResult:
    financials: Mapping[str, FinancialSnapshot]
    derived: Mapping[str, DerivedMetrics]
    ttm_quality_counts: Mapping[str, int]
    yoy_missing_count: int


QUALITY_SIGNAL_MIN_AVAILABLE = 6


@dataclass(frozen=True, slots=True)
class _QualitySignals:
    roa_positive: bool | None
    delta_roa_positive: bool | None
    cfo_positive: bool | None
    accrual_healthy: bool | None
    delta_operating_margin_positive: bool | None
    delta_equity_ratio_positive: bool | None
    no_dilution: bool | None
    delta_asset_turnover_positive: bool | None
    available_count: int
    count: int | None


@dataclass(frozen=True, slots=True)
class ShareholderReturnChangeSignals:
    """Point-in-time facts used only by the calibration panel."""

    dps_streak_up: bool | None
    dps_yoy_latest: float | None
    dps_guidance_up: bool | None
    dividend_initiation: bool | None
    share_count_reduction_streak: int | None
    shareholder_return_change: bool | None


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizedProfitSignals:
    """Split-safe multi-FY earnings facts used only by calibration."""

    normalized_per_3fy: float | None
    normalized_per_5fy: float | None
    eps_cycle_percentile_3fy: float | None
    eps_cycle_peak_3fy: bool | None


def build_normalized_profit_signals(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsDailyBar],
    asof_date: date,
    *,
    close: float | None,
    current_eps: float | None,
) -> NormalizedProfitSignals:
    """Build the preregistered 3/5-FY EPS anchors without filling missing years."""
    available = sorted(
        (summary for summary in summaries if summary.disclosed_at <= asof_date),
        key=lambda item: item.disclosed_at,
    )
    normalized = _normalize_summaries_to_asof_basis(available, ticker_bars, asof_date)
    fy_rows = _latest_fy_revisions(normalized)
    eps_values = {
        row.fiscal_year_end: row.eps_ttm
        for row in fy_rows
        if row.fiscal_year_end is not None and row.eps_ttm is not None
    }
    latest_three = _latest_consecutive_values(fy_rows, eps_values, count=3)
    latest_five = _latest_consecutive_values(fy_rows, eps_values, count=5)

    def normalized_per(values: tuple[float, ...] | None) -> float | None:
        if values is None or close is None or close <= 0:
            return None
        average = fmean(values)
        return close / average if isfinite(average) and average > 0 else None

    percentile: float | None = None
    peak: bool | None = None
    if latest_three is not None and current_eps is not None and isfinite(current_eps):
        below = sum(value < current_eps for value in latest_three)
        equal = sum(value == current_eps for value in latest_three)
        percentile = (below + 0.5 * equal) / len(latest_three)
        peak = percentile >= 0.8

    return NormalizedProfitSignals(
        normalized_per_3fy=normalized_per(latest_three),
        normalized_per_5fy=normalized_per(latest_five),
        eps_cycle_percentile_3fy=percentile,
        eps_cycle_peak_3fy=peak,
    )


def build_metrics(
    asof_date: date,
    securities_by_ticker: Mapping[str, SecurityMaster],
    bars_by_ticker: Mapping[str, Sequence[JQuantsDailyBar]],
    summaries_by_ticker: Mapping[str, Sequence[JQuantsFinancialSummary]],
    edinet_by_ticker: Mapping[str, EdinetMetricRecord],
    rules: ScreeningRules | None = None,
    median_population: frozenset[str] | None = None,
    margin_latest: Mapping[str, JQuantsWeeklyMargin] | None = None,
    margin_prior_26w: Mapping[str, JQuantsWeeklyMargin] | None = None,
    valuation_history_sessions: int = VALUATION_HISTORY_SESSIONS,
) -> MetricBuildResult:
    """Build per-ticker financial and derived metrics for the screen scope.

    ``margin_latest`` / ``margin_prior_26w`` carry the weekly margin balances that
    were already published at ``asof_date``; leaving them out yields the same
    metrics with the supply/demand axes unset, which is what a store without the
    weekly source produces.

    ``median_population`` restricts the comparison population for sector / market
    medians and sector relative strength to the given tickers (the investable,
    liquid set), while metrics are still computed for every ticker in
    ``securities_by_ticker``. This keeps relative-valuation judgments anchored
    to investable comparables even though the screen covers all common stocks;
    ``None`` uses the full scope as the population.
    """
    rules = rules or load_screening_rules()
    financials: dict[str, FinancialSnapshot] = {}
    latest_prices: dict[str, float] = {}

    for ticker, security in securities_by_ticker.items():
        del security
        latest_bar = _latest_bar_on_or_before(bars_by_ticker.get(ticker, ()), asof_date)
        if latest_bar is None:
            continue
        latest_prices[ticker] = latest_bar.close
        ticker_bars = bars_by_ticker.get(ticker, ())
        financials[ticker] = _build_financial_snapshot(
            latest_price=latest_bar.close,
            summaries=_normalize_summaries_to_asof_basis(
                summaries_by_ticker.get(ticker, ()),
                ticker_bars,
                asof_date,
            ),
            edinet=edinet_by_ticker.get(ticker),
            rules=rules,
            ticker_bars=ticker_bars,
            asof_date=asof_date,
        )

    def _in_population(ticker: str) -> bool:
        return median_population is None or ticker in median_population

    sector_metric_values: dict[str, dict[str, list[float]]] = {}
    for ticker, snapshot in financials.items():
        if not _in_population(ticker):
            continue
        sector = securities_by_ticker[ticker].sector_33
        sector_bucket = sector_metric_values.setdefault(
            sector, {metric: [] for metric in VALUATION_METRICS}
        )
        for metric in VALUATION_METRICS:
            value = getattr(snapshot, metric)
            if value is not None:
                sector_bucket[metric].append(value)

    market_metric_values = {
        metric: [
            getattr(snapshot, metric)
            for ticker, snapshot in financials.items()
            if _in_population(ticker) and getattr(snapshot, metric) is not None
        ]
        for metric in VALUATION_METRICS
    }

    sector_returns: dict[str, list[float]] = {}
    ticker_returns_4w: dict[str, float] = {}
    for ticker, security in securities_by_ticker.items():
        four_week = _price_change(bars_by_ticker.get(ticker, ()), 20, asof_date)
        if four_week is not None:
            ticker_returns_4w[ticker] = four_week
            if _in_population(ticker):
                sector_returns.setdefault(security.sector_33, []).append(four_week)

    population_returns_4w = [
        value for ticker, value in ticker_returns_4w.items() if _in_population(ticker)
    ]
    market_return_4w = mean(population_returns_4w) if population_returns_4w else None
    sector_rs = {
        sector: (mean(values) - market_return_4w)
        if values and market_return_4w is not None
        else None
        for sector, values in sector_returns.items()
    }
    rs_percentiles = _rank_to_percentiles(sector_rs)

    history_window_start = asof_date - timedelta(days=PRICE_HISTORY_WINDOW_DAYS)
    price_history_sessions: dict[str, int] = {
        ticker: sum(
            1
            for bar in bars_by_ticker.get(ticker, ())
            if history_window_start < bar.traded_at <= asof_date
        )
        for ticker in financials
    }
    # The densest ticker in scope approximates the full trading calendar for the
    # window, so coverage is a population-relative ratio with no calendar fetch.
    max_history_sessions = max(price_history_sessions.values(), default=0)

    derived: dict[str, DerivedMetrics] = {}
    for ticker, snapshot in financials.items():
        sector = securities_by_ticker[ticker].sector_33
        ticker_bars = bars_by_ticker.get(ticker, ())
        valuation_history = _valuation_history(
            latest_prices[ticker],
            ticker_bars,
            snapshot,
            asof_date,
            history_sessions=valuation_history_sessions,
        )
        sector_gaps: dict[str, float | None] = {}
        sector_medians: dict[str, float | None] = {}
        self_percentiles: dict[str, float | None] = {}
        self_medians: dict[str, float | None] = {}
        sigma_gaps: dict[str, float | None] = {}
        for metric in VALUATION_METRICS:
            current = getattr(snapshot, metric)
            sector_values = sector_metric_values.get(sector, {}).get(metric, [])
            baseline = (
                sector_values if len(sector_values) >= 10 else market_metric_values.get(metric, [])
            )
            sector_median = median(baseline) if baseline else None
            sector_medians[metric] = sector_median
            sector_gaps[metric] = (
                ((current / sector_median) - 1.0)
                if current is not None and sector_median not in (None, 0)
                else None
            )
            history_values = valuation_history.get(metric, [])
            self_percentiles[metric] = _self_range_percentile(history_values, current)
            # 自己レンジの中央値倍率。機械 E[r] の保守側 anchor に使う。標本が薄い
            # 履歴 (直近上場等) の中央値は anchor として不安定なため 100 本を下限にする。
            self_medians[metric] = median(history_values) if len(history_values) >= 100 else None
            sigma_gaps[metric] = _sigma_gap(history_values, current)

        eligible_bars = sorted(
            (bar for bar in ticker_bars if bar.traded_at <= asof_date),
            key=lambda item: item.traded_at,
        )
        listing_span_days = (asof_date - eligible_bars[0].traded_at).days if eligible_bars else 0
        derived[ticker] = DerivedMetrics(
            sector_median_gap=sector_gaps,
            sector_median_value=sector_medians,
            self_range_percentile=self_percentiles,
            self_range_median=self_medians,
            price_change_1d=_price_change(ticker_bars, 1, asof_date),
            price_change_5d=_price_change(ticker_bars, 5, asof_date),
            price_change_20d=_price_change(ticker_bars, 20, asof_date),
            price_change_60d=_price_change(ticker_bars, 60, asof_date),
            realized_volatility_60d=_realized_volatility(ticker_bars, 60, asof_date),
            gap_from_52w_low=_gap_from_low(ticker_bars, 252, asof_date),
            turnover_spike_5d=_turnover_spike(ticker_bars, asof_date),
            sigma_gap=sigma_gaps,
            sector_relative_strength_4w=sector_rs.get(sector),
            sector_relative_strength_percentile=rs_percentiles.get(sector),
            ticker_return_4w=ticker_returns_4w.get(ticker),
            sector_return_4w=mean(sector_returns[sector]) if sector in sector_returns else None,
            short_history_flag=listing_span_days < PRICE_HISTORY_WINDOW_DAYS,
            split_adjustment_flag=_has_split_adjustment_within_sessions(ticker_bars, asof_date, 60),
            **asdict(
                margin_supply_demand(
                    latest=(margin_latest or {}).get(ticker),
                    prior_26w=(margin_prior_26w or {}).get(ticker),
                    avg_daily_volume_shares=_avg_daily_volume(ticker_bars, asof_date),
                    shares_outstanding=snapshot.shares_outstanding,
                    split_within_adv_window=_has_split_adjustment_within_sessions(
                        ticker_bars, asof_date, AVG_VOLUME_SESSIONS
                    ),
                    split_within_delta_window=_has_split_adjustment_within_sessions(
                        ticker_bars, asof_date, MARGIN_DELTA_SESSIONS
                    ),
                )
            ),
            price_history_sessions_750d=price_history_sessions[ticker],
            price_history_coverage_750d=(
                price_history_sessions[ticker] / max_history_sessions
                if max_history_sessions > 0
                else None
            ),
        )

    ttm_quality_counts = _count_ttm_qualities(list(financials.values()))
    yoy_missing_count = sum(
        1
        for snapshot in financials.values()
        if snapshot.eps_yoy is None
        or snapshot.sales_yoy is None
        or snapshot.operating_profit_yoy is None
    )
    return MetricBuildResult(
        financials=financials,
        derived=derived,
        ttm_quality_counts=ttm_quality_counts,
        yoy_missing_count=yoy_missing_count,
    )


def _cumulative_adjustment_factor_after(
    ticker_bars: Sequence[JQuantsDailyBar],
    after: date,
    asof_date: date,
) -> float:
    """`after` より後・asof 以前の bar の adjustment_factor の累積を返す。"""
    factor = 1.0
    for bar in ticker_bars:
        if bar.traded_at <= after or bar.traded_at > asof_date:
            continue
        if bar.adjustment_factor in (None, 0.0, 1.0):
            continue
        assert bar.adjustment_factor is not None
        factor *= bar.adjustment_factor
    return factor


def _normalize_summaries_to_asof_basis(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsDailyBar],
    asof_date: date,
) -> Sequence[JQuantsFinancialSummary]:
    """financial summary 行の per-share 値と株数を asof 時点の株式基準へ換算する。

    J-Quants の財務開示行は as-reported (分割の遡及調整なし) のため、開示後に
    分割・併合 (権利落ち bar の adjustment_factor) があると、行同士の比較・合成
    (TTM 合成・YoY・株数変化) や「開示時点株数 x 権利落ち後価格」の market cap で
    per-share 基準が混在する (1:2 分割なら時価総額が半分・合成 EPS が過大に見える)。
    各行の開示日より後・asof 以前の adjustment_factor を累積し、実績系の
    per-share 値 (eps_ttm / bps) には掛け、株数には割って現在基準へ揃える。
    実績系と株数は同一行内で開示日基準に揃っている (1899/1911 の実データで確認) が、
    forecast_eps だけは「会社が分割考慮後の値で開示する」慣行が混在し、開示時点の
    基準を機械では判別できない。分割を跨ぐ行の forecast_eps は正規化せず None に
    落とす (偽の per_forward を出さない。split_adjustment_recent risk tag が
    research の手動検算へ誘導する)。
    """
    has_adjustment = any(
        bar.adjustment_factor not in (None, 0.0, 1.0)
        for bar in ticker_bars
        if bar.traded_at <= asof_date
    )
    if not has_adjustment:
        return summaries
    normalized: list[JQuantsFinancialSummary] = []
    for summary in summaries:
        factor = _cumulative_adjustment_factor_after(ticker_bars, summary.disclosed_at, asof_date)
        if factor <= 0 or factor == 1.0:
            normalized.append(summary)
            continue
        normalized.append(
            replace(
                summary,
                eps_ttm=summary.eps_ttm * factor if summary.eps_ttm is not None else None,
                forecast_eps=None,
                # 実績 DPS は per-share 実績と同じ換算。予想 DPS は forecast_eps と
                # 同じ基準判別不能問題を持つため分割跨ぎ行では None に落とす。
                dps_actual_annual=(
                    summary.dps_actual_annual * factor
                    if summary.dps_actual_annual is not None
                    else None
                ),
                dps_forecast_annual=None,
                bps=summary.bps * factor if summary.bps is not None else None,
                shares_outstanding=(
                    summary.shares_outstanding / factor
                    if summary.shares_outstanding is not None
                    else None
                ),
            )
        )
    return normalized


def build_shares_outstanding_index(
    summaries_by_ticker: Mapping[str, Sequence[JQuantsFinancialSummary]],
    bars_by_ticker: Mapping[str, Sequence[JQuantsDailyBar]],
    asof_date: date,
) -> dict[str, float | None]:
    shares: dict[str, float | None] = {}
    for ticker, summaries in summaries_by_ticker.items():
        normalized = _normalize_summaries_to_asof_basis(
            summaries, bars_by_ticker.get(ticker, ()), asof_date
        )
        latest = _latest_summary(normalized)
        shares[ticker] = latest.shares_outstanding if latest else None
    return shares


def group_bars_by_ticker(bars: Sequence[JQuantsDailyBar]) -> dict[str, list[JQuantsDailyBar]]:
    grouped: dict[str, list[JQuantsDailyBar]] = {}
    for bar in bars:
        grouped.setdefault(bar.ticker, []).append(bar)
    return grouped


def group_summaries_by_ticker(
    summaries: Sequence[JQuantsFinancialSummary],
) -> dict[str, list[JQuantsFinancialSummary]]:
    grouped: dict[str, list[JQuantsFinancialSummary]] = {}
    for summary in summaries:
        grouped.setdefault(summary.ticker, []).append(summary)
    for ticker in grouped:
        grouped[ticker].sort(key=lambda item: item.disclosed_at)
    return grouped


@dataclass(frozen=True, slots=True, kw_only=True)
class _DividendCarry:
    """carry 用に解決した配当利回りと、その基準・分割 factor の事実記録。"""

    dividend_yield: float | None
    dps_actual_annual: float | None
    dps_forecast_annual: float | None
    basis: str
    split_factor: float | None


def _actual_dps_rows(
    summaries: Sequence[JQuantsFinancialSummary],
) -> list[JQuantsFinancialSummary]:
    return [
        summary
        for summary in sorted(summaries, key=lambda item: item.disclosed_at, reverse=True)
        if summary.dps_actual_annual is not None
    ]


def _resolve_dividend_carry(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsDailyBar],
    latest_price: float,
    asof_date: date,
) -> _DividendCarry:
    """carry 用の配当利回りと基準を解決する。

    通期実績 DPS は accrual 期間 (前期通期実績開示 〜 当期通期実績開示) で積み上がる。
    その期間内かつ開示前に分割が起きると、正規化 (各行の開示日基準) では捕捉できず、
    実績 DPS が分割前・株価が分割後の混在になり配当利回りが factor 倍に膨らむ。
    carry は将来利回りなので、分割後基準で開示される予想 DPS を最優先し、無ければ
    accrual 期間の累積分割 factor で実績 DPS を分割後基準へ調整する。予想・実績とも
    正の値が取れなければ利回りは None (buyback のみが carry に残る)。
    """
    del asof_date  # accrual 窓は実績開示日を基準に閉じるため asof は使わない
    forecast = _latest_non_null(summaries, "dps_forecast_annual")

    actual_rows = _actual_dps_rows(summaries)
    split_factor = 1.0
    actual_split_safe: float | None = None
    if actual_rows:
        latest_actual = actual_rows[0]
        assert latest_actual.dps_actual_annual is not None
        # accrual 開始 = 前期の通期実績開示日。無ければ開示日から約 1 年遡る。
        accrual_start = (
            actual_rows[1].disclosed_at
            if len(actual_rows) > 1
            else latest_actual.disclosed_at - timedelta(days=DIVIDEND_ACCRUAL_LOOKBACK_DAYS)
        )
        # 正規化は開示日「後」の分割のみ反映済み。ここでは accrual 開始〜開示日の
        # (開示前) 分割の差分 factor だけを掛け、二重計上を避ける。
        split_factor = _cumulative_adjustment_factor_after(
            ticker_bars, accrual_start, latest_actual.disclosed_at
        )
        actual_split_safe = latest_actual.dps_actual_annual * split_factor

    recorded_factor = split_factor if split_factor != 1.0 else None

    if latest_price <= 0:
        basis = "unavailable"
    elif forecast is not None and forecast > 0:
        return _DividendCarry(
            dividend_yield=forecast / latest_price,
            dps_actual_annual=actual_split_safe,
            dps_forecast_annual=forecast,
            basis="forecast_annual",
            split_factor=recorded_factor,
        )
    elif actual_split_safe is not None and actual_split_safe > 0:
        return _DividendCarry(
            dividend_yield=actual_split_safe / latest_price,
            dps_actual_annual=actual_split_safe,
            dps_forecast_annual=forecast,
            basis="actual_split_adjusted" if split_factor != 1.0 else "actual_reported",
            split_factor=recorded_factor,
        )
    else:
        basis = "unavailable"

    return _DividendCarry(
        dividend_yield=None,
        dps_actual_annual=actual_split_safe,
        dps_forecast_annual=forecast,
        basis=basis,
        split_factor=recorded_factor,
    )


def build_shareholder_return_change_signals(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsDailyBar],
    asof_date: date,
) -> ShareholderReturnChangeSignals:
    """Build preregistered shareholder-return change facts for calibration.

    Revisions are resolved before field availability is inspected: a null in the
    newest revision cannot silently fall back to an older value. DPS and gross
    share counts are normalized to the cohort's stock basis, while the latter is
    deliberately described as share-count reduction rather than proof of a
    buyback because the source includes treasury stock.
    """
    available = sorted(
        (summary for summary in summaries if summary.disclosed_at <= asof_date),
        key=lambda item: item.disclosed_at,
    )
    normalized = _normalize_summaries_to_asof_basis(available, ticker_bars, asof_date)
    fy_rows = _latest_fy_revisions(normalized)

    dps_values = _split_safe_fy_dps(fy_rows, ticker_bars)
    latest_pair = _latest_consecutive_values(fy_rows, dps_values, count=2)
    latest_three = _latest_consecutive_values(fy_rows, dps_values, count=3)

    dps_streak_up = (
        latest_three[0] <= latest_three[1] <= latest_three[2] if latest_three is not None else None
    )
    actual_up: bool | None = None
    dps_yoy_latest: float | None = None
    if latest_pair is not None:
        prior_dps, latest_dps = latest_pair
        actual_up = latest_dps > prior_dps
        if prior_dps > 0:
            dps_yoy_latest = (latest_dps / prior_dps) - 1.0
        elif latest_dps == 0:
            dps_yoy_latest = 0.0

    latest_actual_row = _latest_row_with_value(fy_rows, dps_values)
    if latest_actual_row is not None:
        assert latest_actual_row.fiscal_year_end is not None
        latest_actual = dps_values[latest_actual_row.fiscal_year_end]
    else:
        latest_actual = None
    forecast = _latest_forecast_after(normalized, latest_actual_row)
    dps_guidance_up = (
        forecast > latest_actual if forecast is not None and latest_actual is not None else None
    )
    dividend_initiation = _dividend_initiation(
        latest_pair=latest_pair,
        latest_actual=latest_actual,
        forecast=forecast,
    )

    share_values = {
        row.fiscal_year_end: row.shares_outstanding
        for row in fy_rows
        if row.fiscal_year_end is not None
        and row.shares_outstanding is not None
        and row.shares_outstanding > 0
    }
    latest_share_three = _latest_consecutive_values(fy_rows, share_values, count=3)
    share_count_reduction_streak: int | None = None
    if latest_share_three is not None:
        share_count_reduction_streak = 0
        for prior, current in reversed(tuple(pairwise(latest_share_three))):
            if current >= prior:
                break
            share_count_reduction_streak += 1

    change_components = (
        actual_up,
        dps_guidance_up,
        dividend_initiation,
        (share_count_reduction_streak >= 1 if share_count_reduction_streak is not None else None),
    )
    if any(value is True for value in change_components):
        shareholder_return_change: bool | None = True
    elif all(value is False for value in change_components):
        shareholder_return_change = False
    else:
        shareholder_return_change = None

    return ShareholderReturnChangeSignals(
        dps_streak_up=dps_streak_up,
        dps_yoy_latest=dps_yoy_latest,
        dps_guidance_up=dps_guidance_up,
        dividend_initiation=dividend_initiation,
        share_count_reduction_streak=share_count_reduction_streak,
        shareholder_return_change=shareholder_return_change,
    )


def _latest_fy_revisions(
    summaries: Sequence[JQuantsFinancialSummary],
) -> list[JQuantsFinancialSummary]:
    latest: dict[date, JQuantsFinancialSummary] = {}
    for summary in summaries:
        if summary.fiscal_period != "FY" or summary.fiscal_year_end is None:
            continue
        previous = latest.get(summary.fiscal_year_end)
        if previous is None or summary.disclosed_at > previous.disclosed_at:
            latest[summary.fiscal_year_end] = summary
    return [latest[fiscal_year_end] for fiscal_year_end in sorted(latest)]


def _split_safe_fy_dps(
    fy_rows: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsDailyBar],
) -> dict[date, float]:
    values: dict[date, float] = {}
    for index, row in enumerate(fy_rows):
        fiscal_year_end = row.fiscal_year_end
        value = row.dps_actual_annual
        if fiscal_year_end is None or value is None or value < 0:
            continue
        prior = fy_rows[index - 1] if index > 0 else None
        accrual_start = (
            prior.disclosed_at
            if prior is not None
            and prior.dps_actual_annual is not None
            and prior.dps_actual_annual >= 0
            else row.disclosed_at - timedelta(days=DIVIDEND_ACCRUAL_LOOKBACK_DAYS)
        )
        factor = _cumulative_adjustment_factor_after(ticker_bars, accrual_start, row.disclosed_at)
        if factor > 0:
            values[fiscal_year_end] = value * factor
    return values


def _latest_consecutive_values(
    fy_rows: Sequence[JQuantsFinancialSummary],
    values: Mapping[date, float],
    *,
    count: int,
) -> tuple[float, ...] | None:
    if len(fy_rows) < count:
        return None
    selected = list(fy_rows[-count:])
    for prior, current in pairwise(selected):
        if current.fiscal_year_end is None or prior.fiscal_year_end is None:
            return None
        if _shift_year(current.fiscal_year_end, -1) != prior.fiscal_year_end:
            return None
    resolved: list[float] = []
    for row in selected:
        if row.fiscal_year_end is None or row.fiscal_year_end not in values:
            return None
        resolved.append(values[row.fiscal_year_end])
    return tuple(resolved)


def _latest_row_with_value(
    fy_rows: Sequence[JQuantsFinancialSummary], values: Mapping[date, float]
) -> JQuantsFinancialSummary | None:
    if not fy_rows:
        return None
    row = fy_rows[-1]
    return row if row.fiscal_year_end is not None and row.fiscal_year_end in values else None


def _latest_forecast_after(
    summaries: Sequence[JQuantsFinancialSummary],
    latest_actual_row: JQuantsFinancialSummary | None,
) -> float | None:
    if latest_actual_row is None:
        return None
    for summary in reversed(summaries):
        if summary.disclosed_at < latest_actual_row.disclosed_at:
            break
        value = summary.dps_forecast_annual
        if value is not None and value >= 0:
            return value
    return None


def _dividend_initiation(
    *,
    latest_pair: tuple[float, ...] | None,
    latest_actual: float | None,
    forecast: float | None,
) -> bool | None:
    if latest_pair is not None:
        prior, latest = latest_pair
        if prior == 0 and latest > 0:
            return True
        if latest > 0:
            return False
    if latest_actual == 0 and forecast is not None:
        return forecast > 0
    return None


def _build_financial_snapshot(
    latest_price: float,
    summaries: Sequence[JQuantsFinancialSummary],
    edinet: EdinetMetricRecord | None,
    rules: ScreeningRules,
    *,
    ticker_bars: Sequence[JQuantsDailyBar],
    asof_date: date,
) -> FinancialSnapshot:
    latest = _latest_summary(summaries)
    prior_year = _prior_year_summary(summaries, rules.ttm)
    forecast_eps = latest.forecast_eps if latest else None
    # 会社予想で純利益>経常なら特別益をほぼ確定する 1 行チェック (税負担が通常正)。
    # 純利益/経常は forecast_eps と同一予想期のペアで ingest 済み・分割不変の絶対額なので、
    # 両方揃うときだけ比較する。flag は warning で per_forward / E[r] / rank を変えない。
    forecast_profit = latest.forecast_profit if latest else None
    forecast_ordinary_profit = latest.forecast_ordinary_profit if latest else None
    forecast_special_gain_flag = (
        forecast_profit is not None
        and forecast_ordinary_profit is not None
        and forecast_profit > forecast_ordinary_profit
    )
    # J-Quants の EPS (eps_ttm field) は期中累計で、年度途中の四半期開示では 12 か月分に
    # ならない (Q1 開示だと 3 か月分)。sales / cfo と同じ rolling 合成
    # (直近累計 + 前期通期 - 前年同期間累計) で TTM に直し、通期開示のときだけ
    # そのまま使う。合成できない場合は per_trailing を出さない (単一四半期 EPS で
    # 割った偽の割高 PER を作らない)。分割を跨ぐ行の per-share 基準は
    # _normalize_summaries_to_asof_basis が呼び出し側で揃えている前提。
    eps_cumulative = latest.eps_ttm if latest else None
    eps_ttm, eps_quality = _ttm_value(summaries, "eps_ttm", rules.ttm)
    # BS 系 fact (bps / cash_eq / equity / total_assets / 株数) は四半期開示に
    # 載らないことが多く (bps 非 null は FY 開示 ~69% に対し四半期 ~17-20%)、
    # latest 行だけを見ると四半期行が最新になる断面で PBR 等が季節的に大量欠損
    # する。直近の非 null 行から carry-forward し (値は asof-basis 正規化済み)、
    # どの field をいつの開示から引いたかを staleness fact として残す。
    bps, bps_lag = _carry_forward(summaries, "bps", latest)
    cash_eq, cash_eq_lag = _carry_forward(summaries, "cash_eq", latest)
    equity, equity_lag = _carry_forward(summaries, "equity", latest)
    total_assets, total_assets_lag = _carry_forward(summaries, "total_assets", latest)
    carried_lags = {
        "bps": bps_lag,
        "cash_eq": cash_eq_lag,
        "equity": equity_lag,
        "total_assets": total_assets_lag,
    }
    bs_carry_forward_fields = ",".join(
        sorted(name for name, lag in carried_lags.items() if lag is not None and lag > 0)
    )
    bs_carry_forward_lag_days = max(
        (lag for lag in carried_lags.values() if lag is not None), default=None
    )
    # carry 用の配当利回りは予想 DPS (分割後基準・特別配当を含まない前提) を最優先し、
    # 無ければ accrual 期間の分割 factor で調整した実績 DPS を使う。実績 DPS は
    # FY 開示にしか載らないため直近の非 null 行から取り、split-safe 化した値を
    # snapshot の実績 DPS として記録する (株価と同じ分割後基準で表示・比較できる)。
    dividend = _resolve_dividend_carry(summaries, ticker_bars, latest_price, asof_date)
    dps_actual_annual = dividend.dps_actual_annual
    dps_forecast_annual = dividend.dps_forecast_annual
    dividend_yield = dividend.dividend_yield
    per_forward = (latest_price / forecast_eps) if forecast_eps and forecast_eps > 0 else None
    per_trailing = (latest_price / eps_ttm) if eps_ttm and eps_ttm > 0 else None
    pbr = (latest_price / bps) if bps and bps > 0 else None
    operating_profit, operating_profit_source = _select_operating_profit(latest)
    operating_profit_prior_year, _ = _select_operating_profit(prior_year)
    shares_outstanding, _shares_lag = _carry_forward(summaries, "shares_outstanding", latest)
    sales_ttm, sales_quality = _ttm_value(summaries, "sales", rules.ttm)
    ocf_ttm, ocf_quality = _ttm_value(summaries, "cfo", rules.ttm)
    edinet_ocf_ttm = edinet.ocf_ttm if edinet else None
    debt = edinet.debt if edinet else None
    cash = edinet.cash if edinet else None
    ebitda_ttm = edinet.ebitda_ttm if edinet else None
    fcf_ttm = edinet.fcf_ttm if edinet else None
    net_cash = edinet.net_cash if edinet else None
    investment_securities = edinet.investment_securities if edinet else None
    edinet_failure_reasons = ",".join(edinet.failure_reasons) if edinet else None
    if net_cash is None and cash is not None and debt is not None:
        net_cash = cash - debt
    latest_market_cap = (latest_price * shares_outstanding) if shares_outstanding else None
    latest_enterprise_value = (
        (latest_market_cap + debt - cash)
        if latest_market_cap is not None and debt is not None and cash is not None
        else None
    )
    ev_ebitda = _safe_positive_ratio(latest_enterprise_value, ebitda_ttm)
    accruals_to_assets = _accruals_to_assets(
        eps_ttm=eps_ttm,
        shares=shares_outstanding,
        ocf_ttm=ocf_ttm,
        total_assets=total_assets,
        prior_total_assets=prior_year.total_assets if prior_year else None,
    )
    net_share_change_yoy = _yoy_ratio(
        shares_outstanding,
        prior_year.shares_outstanding if prior_year else None,
    )
    quality = _build_quality_signals(
        summaries,
        prior_year=prior_year,
        ttm_rules=rules.ttm,
        total_assets=total_assets,
        equity=equity,
        accruals_to_assets=accruals_to_assets,
        net_share_change_yoy=net_share_change_yoy,
    )

    return FinancialSnapshot(
        per_forward=per_forward,
        per_trailing=per_trailing,
        pbr=pbr,
        ev_ebitda=ev_ebitda,
        p_s=_safe_ratio(latest_market_cap, sales_ttm),
        pcfr=_safe_ratio(latest_market_cap, ocf_ttm if ocf_ttm and ocf_ttm > 0 else None),
        eps=eps_ttm,
        dps_actual_annual=dps_actual_annual,
        dps_forecast_annual=dps_forecast_annual,
        dividend_yield=dividend_yield,
        dividend_basis=dividend.basis,
        dividend_split_factor=dividend.split_factor,
        sales_ttm=sales_ttm,
        ocf_ttm=ocf_ttm,
        edinet_ocf_ttm=edinet_ocf_ttm,
        sales=latest.sales if latest else None,
        cfo=latest.cfo if latest else None,
        cash_eq=cash_eq,
        total_assets=total_assets,
        equity=equity,
        market_cap=latest_market_cap,
        cash_to_market_cap=_safe_ratio(cash_eq, latest_market_cap),
        price_to_equity=_safe_ratio(latest_market_cap, equity),
        equity_ratio=_safe_ratio(equity, total_assets),
        ocf_yield=_safe_ratio(ocf_ttm, latest_market_cap),
        net_cash=net_cash,
        net_cash_to_market_cap=_safe_ratio(net_cash, latest_market_cap),
        investment_securities=investment_securities,
        asset_backed_ratio=_safe_ratio(
            (
                net_cash + investment_securities
                if net_cash is not None and investment_securities is not None
                else None
            ),
            latest_market_cap,
        ),
        fcf_ttm=fcf_ttm,
        fcf_yield=_safe_ratio(fcf_ttm, latest_market_cap),
        capex_ttm=edinet.capex_ttm if edinet else None,
        depreciation_and_amortization_ttm=(
            edinet.depreciation_and_amortization_ttm if edinet else None
        ),
        debt=debt,
        cash=cash,
        ebitda_ttm=ebitda_ttm,
        consolidation_basis=edinet.consolidation_basis if edinet else None,
        edinet_source_doc_id=edinet.source_doc_id if edinet else None,
        edinet_document_type=edinet.document_type if edinet else None,
        edinet_source_submit_datetime=edinet.source_submit_datetime if edinet else None,
        edinet_source_period_start=edinet.source_period_start if edinet else None,
        edinet_source_period_end=edinet.source_period_end if edinet else None,
        edinet_capex_source=edinet.capex_source if edinet else None,
        edinet_failure_reasons=edinet_failure_reasons or None,
        operating_profit=operating_profit,
        operating_profit_source=operating_profit_source,
        eps_yoy=_yoy_ratio(eps_cumulative, prior_year.eps_ttm if prior_year else None),
        sales_yoy=_yoy_ratio(
            latest.sales if latest else None, prior_year.sales if prior_year else None
        ),
        operating_profit_yoy=_yoy_ratio(operating_profit, operating_profit_prior_year),
        cfo_yoy=_yoy_ratio(latest.cfo if latest else None, prior_year.cfo if prior_year else None),
        operating_profit_loss_narrowing=_loss_narrowing(
            operating_profit,
            operating_profit_prior_year,
        ),
        ttm_quality_ev_ebitda=edinet.ttm_quality_ev_ebitda if edinet else TTMQuality.UNAVAILABLE,
        ttm_quality_per_trailing=eps_quality,
        ttm_quality_p_s=sales_quality,
        ttm_quality_pcfr=ocf_quality,
        ttm_quality_ocf_yield=ocf_quality,
        ttm_quality_sales=sales_quality,
        ttm_quality_fcf_yield=edinet.ttm_quality_fcf if edinet else TTMQuality.UNAVAILABLE,
        ttm_quality_net_cash=edinet.ttm_quality_net_cash if edinet else TTMQuality.UNAVAILABLE,
        shares_outstanding=shares_outstanding,
        accruals_to_assets=accruals_to_assets,
        net_share_change_yoy=net_share_change_yoy,
        quality_roa_positive=quality.roa_positive,
        quality_delta_roa_positive=quality.delta_roa_positive,
        quality_cfo_positive=quality.cfo_positive,
        quality_accrual_healthy=quality.accrual_healthy,
        quality_delta_operating_margin_positive=quality.delta_operating_margin_positive,
        quality_delta_equity_ratio_positive=quality.delta_equity_ratio_positive,
        quality_no_dilution=quality.no_dilution,
        quality_delta_asset_turnover_positive=quality.delta_asset_turnover_positive,
        quality_signal_available_count=quality.available_count,
        quality_signal_count=quality.count,
        bs_carry_forward_fields=bs_carry_forward_fields or None,
        bs_carry_forward_lag_days=bs_carry_forward_lag_days,
        forecast_special_gain_flag=forecast_special_gain_flag,
    )


def _latest_non_null(summaries: Sequence[JQuantsFinancialSummary], field_name: str) -> float | None:
    for summary in sorted(summaries, key=lambda item: item.disclosed_at, reverse=True):
        value = getattr(summary, field_name)
        if value is not None:
            return float(value)
    return None


def _carry_forward(
    summaries: Sequence[JQuantsFinancialSummary],
    field_name: str,
    latest: JQuantsFinancialSummary | None,
) -> tuple[float | None, int | None]:
    """直近の非 null 行から値を取り、latest 行からの遅延日数を添える。

    lag 0 = latest 行自身が値を持つ。lag > 0 = carry-forward された staleness。
    値が窓内のどの行にも無ければ (None, None)。
    """
    if latest is None:
        return None, None
    for summary in sorted(summaries, key=lambda item: item.disclosed_at, reverse=True):
        value = getattr(summary, field_name)
        if value is not None:
            return float(value), (latest.disclosed_at - summary.disclosed_at).days
    return None, None


def _latest_summary(summaries: Sequence[JQuantsFinancialSummary]) -> JQuantsFinancialSummary | None:
    return summaries[-1] if summaries else None


def _prior_year_summary(
    summaries: Sequence[JQuantsFinancialSummary],
    ttm_rules: TTMRules,
) -> JQuantsFinancialSummary | None:
    """Return the same fiscal period in the previous fiscal year.

    Assumes summaries are ordered oldest-first; revisions of the same fiscal
    period are resolved by taking the most recent occurrence.
    """
    latest = _latest_summary(summaries)
    if latest is None:
        return None
    if latest.period_start is not None and latest.period_end is not None:
        matched = _matched_prior_period_summary(summaries, latest, ttm_rules)
        if matched is not None:
            return matched
    if latest.fiscal_period is None or latest.fiscal_year_end is None:
        return None
    target_fiscal_year_end = _shift_year(latest.fiscal_year_end, -1)
    if target_fiscal_year_end is None:
        return None
    candidates = [
        summary
        for summary in summaries[:-1]
        if summary.fiscal_period == latest.fiscal_period
        and summary.fiscal_year_end == target_fiscal_year_end
    ]
    return candidates[-1] if candidates else None


def _ttm_value(
    summaries: Sequence[JQuantsFinancialSummary],
    field: str,
    ttm_rules: TTMRules,
) -> tuple[float | None, TTMQuality]:
    latest = _latest_summary(summaries)
    if latest is None or latest.period_start is None or latest.period_end is None:
        return None, TTMQuality.UNAVAILABLE
    latest_value = getattr(latest, field)
    if latest_value is None:
        return None, TTMQuality.UNAVAILABLE
    latest_days = _period_days(latest)
    if latest_days is None:
        return None, TTMQuality.UNAVAILABLE
    if ttm_rules.full_year_min_days <= latest_days <= ttm_rules.full_year_max_days:
        return latest_value, TTMQuality.EXACT
    if latest.fiscal_year_end is None:
        return None, TTMQuality.UNAVAILABLE
    prior_fy_end = _shift_year(latest.fiscal_year_end, -1)
    if prior_fy_end is None:
        return None, TTMQuality.UNAVAILABLE
    prior_fy = _latest_full_year_summary(summaries, prior_fy_end, field, ttm_rules)
    prior_same = _matched_prior_period_summary(summaries, latest, ttm_rules)
    if prior_fy is None or prior_same is None:
        return None, TTMQuality.UNAVAILABLE
    prior_fy_value = getattr(prior_fy, field)
    prior_same_value = getattr(prior_same, field)
    if prior_fy_value is None or prior_same_value is None:
        return None, TTMQuality.UNAVAILABLE
    return latest_value + prior_fy_value - prior_same_value, TTMQuality.EXACT


def _latest_full_year_summary(
    summaries: Sequence[JQuantsFinancialSummary],
    fiscal_year_end: date,
    field: str,
    ttm_rules: TTMRules,
) -> JQuantsFinancialSummary | None:
    candidates = [
        summary
        for summary in summaries
        if summary.fiscal_year_end == fiscal_year_end
        and getattr(summary, field) is not None
        and (days := _period_days(summary)) is not None
        and ttm_rules.full_year_min_days <= days <= ttm_rules.full_year_max_days
    ]
    return candidates[-1] if candidates else None


def _matched_prior_period_summary(
    summaries: Sequence[JQuantsFinancialSummary],
    latest: JQuantsFinancialSummary,
    ttm_rules: TTMRules,
) -> JQuantsFinancialSummary | None:
    if latest.period_start is None or latest.period_end is None:
        return None
    latest_days = _period_days(latest)
    prior_start = _shift_year(latest.period_start, -1)
    prior_end = _shift_year(latest.period_end, -1)
    if latest_days is None or prior_start is None or prior_end is None:
        return None
    max_length_delta = max(1, round(latest_days * ttm_rules.period_length_tolerance_ratio))
    candidates: list[JQuantsFinancialSummary] = []
    for summary in summaries[:-1]:
        if summary.period_start is None or summary.period_end is None:
            continue
        summary_days = _period_days(summary)
        if summary_days is None or abs(summary_days - latest_days) > max_length_delta:
            continue
        if abs((summary.period_end - prior_end).days) > ttm_rules.period_end_tolerance_days:
            continue
        if abs((summary.period_start - prior_start).days) > ttm_rules.period_end_tolerance_days:
            continue
        candidates.append(summary)
    return candidates[-1] if candidates else None


def _period_days(summary: JQuantsFinancialSummary) -> int | None:
    if summary.period_start is None or summary.period_end is None:
        return None
    days = (summary.period_end - summary.period_start).days + 1
    return days if days > 0 else None


def _shift_year(value: date, years: int) -> date | None:
    try:
        return value.replace(year=value.year + years)
    except ValueError:
        # Feb 29 has no same-month/day counterpart in non-leap years; avoid
        # inventing a fiscal year-end match.
        return None


def _latest_bar_on_or_before(
    bars: Sequence[JQuantsDailyBar],
    asof_date: date,
) -> JQuantsDailyBar | None:
    filtered = [bar for bar in bars if bar.traded_at <= asof_date]
    return max(filtered, key=lambda item: item.traded_at) if filtered else None


def _valuation_history(
    latest_price: float,
    bars: Sequence[JQuantsDailyBar],
    snapshot: FinancialSnapshot,
    asof_date: date,
    *,
    history_sessions: int = VALUATION_HISTORY_SESSIONS,
) -> dict[str, list[float]]:
    # asof 以前の bar に限定し、look-ahead bias を防ぐ。
    eligible = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date),
        key=lambda item: item.traded_at,
    )
    # 価格の不連続 (分割) を valuation history に持ち込まないため調整済み系列を使う。
    # cache の adjustment_close は incremental 取得で基準が混在するため使わず、
    # 不変イベントの adjustment_factor から asof 基準の系列を自前で組む。
    prices = asof_basis_closes(eligible[-history_sessions:])
    ev_ebitda_history: list[float] = []
    if (
        snapshot.shares_outstanding is not None
        and snapshot.debt is not None
        and snapshot.cash is not None
        and snapshot.ebitda_ttm is not None
        and snapshot.ebitda_ttm > 0
    ):
        ev_ebitda_history = [
            value
            for price in prices
            if (value := _historical_ev_ebitda(price, snapshot)) is not None
        ]
    pbr_history: list[float] = []
    pbr = snapshot.pbr
    if pbr is not None and pbr != 0:
        pbr_basis = latest_price / pbr
        pbr_history = [price / pbr_basis for price in prices]
    p_s_history: list[float] = []
    if (
        snapshot.sales_ttm is not None
        and snapshot.sales_ttm > 0
        and snapshot.shares_outstanding is not None
    ):
        p_s_history = [
            (price * snapshot.shares_outstanding) / snapshot.sales_ttm for price in prices
        ]

    # Historical forward PER holds forecast EPS constant against adjusted close,
    # matching the trailing-PER history convention and keeping range metrics
    # available for the forward valuation input.
    per_forward_history: list[float] = []
    if snapshot.per_forward is not None and snapshot.per_forward != 0:
        per_forward_basis = latest_price / snapshot.per_forward
        if per_forward_basis > 0:
            per_forward_history = [price / per_forward_basis for price in prices]
    history: dict[str, list[float]] = {
        "per_forward": per_forward_history,
        "per_trailing": [
            (price / snapshot.eps) for price in prices if snapshot.eps and snapshot.eps > 0
        ],
        "pbr": pbr_history,
        "ev_ebitda": ev_ebitda_history,
        "p_s": p_s_history,
    }
    return history


def _historical_ev_ebitda(
    price: float,
    snapshot: FinancialSnapshot,
) -> float | None:
    """Return the EV/EBITDA value for one historical price.

    Caller must ensure ``shares_outstanding`` / ``debt`` / ``cash`` are not
    None and ``ebitda_ttm`` is positive. v1 has only the latest balance sheet
    and TTM EBITDA, so those are held constant across price history while
    market cap varies with the (adjusted) historical close.

    Negative EV or non-positive EBITDA is outside the valuation multiple
    domain and is handled by cash / net-cash playbooks, not by EV/EBITDA mean
    reversion.
    """
    shares_outstanding = snapshot.shares_outstanding
    debt = snapshot.debt
    cash = snapshot.cash
    ebitda_ttm = snapshot.ebitda_ttm
    if shares_outstanding is None or debt is None or cash is None:
        raise ValueError("EV/EBITDA history requires shares, debt, and cash")
    if ebitda_ttm is None or ebitda_ttm <= 0:
        raise ValueError("EV/EBITDA history requires positive EBITDA")
    enterprise_value = (price * shares_outstanding) + debt - cash
    if enterprise_value <= 0:
        return None
    return enterprise_value / ebitda_ttm


def _price_change(bars: Sequence[JQuantsDailyBar], sessions: int, asof_date: date) -> float | None:
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if len(ordered) <= sessions:
        return None
    # 分割を跨ぐ比較で偽の騰落を出さないよう、adjustment_factor から組んだ
    # asof 基準系列で両端を比較する (cache の adjustment_close は基準混在のため不使用)。
    prices = asof_basis_closes(ordered[-(sessions + 1) :])
    base = prices[0]
    if base == 0:
        return None
    return (prices[-1] / base) - 1.0


def _realized_volatility(
    bars: Sequence[JQuantsDailyBar], sessions: int, asof_date: date
) -> float | None:
    """Annualized close-to-close volatility on one as-of-consistent share basis."""
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if len(ordered) <= sessions:
        return None
    prices = asof_basis_closes(ordered[-(sessions + 1) :])
    returns = [
        prices[index] / prices[index - 1] - 1.0
        for index in range(1, len(prices))
        if prices[index - 1] > 0
    ]
    if len(returns) != sessions or len(returns) < 2:
        return None
    mean_return = mean(returns)
    variance = sum((value - mean_return) ** 2 for value in returns) / (len(returns) - 1)
    return sqrt(variance * 252)


def _gap_from_low(
    bars: Sequence[JQuantsDailyBar],
    sessions: int,
    asof_date: date,
) -> float | None:
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if not ordered:
        return None
    prices = asof_basis_closes(ordered[-sessions:])
    current = prices[-1]
    low = min(prices)
    if low <= 0:
        return None
    return (current / low) - 1.0


# A trailing window must be long enough to average out one busy day, and it has to
# tolerate the days an illiquid name simply does not report. Demanding all twenty
# would drop the established thin names where margin overhang matters most.
AVG_VOLUME_SESSIONS = 20
AVG_VOLUME_MIN_OBSERVED = 15


def _avg_daily_volume(
    bars: Sequence[JQuantsDailyBar],
    asof_date: date,
    sessions: int = AVG_VOLUME_SESSIONS,
) -> float | None:
    """Mean traded shares over the trailing sessions, or None when too sparse.

    Shares rather than yen, because it is the denominator that turns a margin
    balance into days of trading; dividing yen turnover by the close would put the
    close where the day's average price belongs. The window must exist in full so a
    newly listed name cannot produce a days-of-volume figure off two sessions, but
    within it a minority of unreported days is averaged over rather than fatal.
    """
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    window = ordered[-sessions:]
    if len(window) < sessions:
        return None
    values = [float(bar.volume) for bar in window if bar.volume is not None]
    if len(values) < AVG_VOLUME_MIN_OBSERVED:
        return None
    return mean(values)


def _turnover_spike(
    bars: Sequence[JQuantsDailyBar],
    asof_date: date,
    latest_sessions: int = 5,
    baseline_sessions: int = 20,
) -> float | None:
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if len(ordered) < latest_sessions + baseline_sessions:
        return None
    latest_values = [
        bar.turnover_value for bar in ordered[-latest_sessions:] if bar.turnover_value is not None
    ]
    baseline_values = [
        bar.turnover_value
        for bar in ordered[-(latest_sessions + baseline_sessions) : -latest_sessions]
        if bar.turnover_value is not None
    ]
    if len(latest_values) < latest_sessions or len(baseline_values) < baseline_sessions:
        return None
    baseline = mean(baseline_values)
    if baseline <= 0:
        return None
    return mean(latest_values) / baseline


def _has_split_adjustment_within_sessions(
    bars: Sequence[JQuantsDailyBar], asof_date: date, sessions: int
) -> bool:
    """True when J-Quants adjustment_factor marks a split in the latest sessions.

    J-Quants daily_quotes sets ``AdjustmentFactor`` on ex-rights dates for
    stock splits and reverse splits. Use the same session window as
    ``price_change_60d`` so the flag covers the price-change calculation it
    qualifies.
    """
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if len(ordered) < 2:
        return False

    relevant = ordered[-(sessions + 1) :]
    factors = [bar.adjustment_factor for bar in relevant if bar.adjustment_factor is not None]
    return any(factor != 1.0 for factor in factors) or len(set(factors)) > 1


def _self_range_percentile(history: Sequence[float], current: float | None) -> float | None:
    if not history or current is None:
        return None
    low = min(history)
    high = max(history)
    if high == low:
        return 0.0
    return (current - low) / (high - low)


def _sigma_gap(history: Sequence[float], current: float | None) -> float | None:
    if current is None or len(history) < 2:
        return None
    avg = mean(history)
    variance = sum((value - avg) ** 2 for value in history) / len(history)
    stddev = sqrt(variance)
    if stddev == 0:
        return 0.0
    return (current - avg) / stddev


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _safe_positive_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or numerator <= 0 or denominator <= 0:
        return None
    return numerator / denominator


def _select_operating_profit(
    summary: JQuantsFinancialSummary | None,
) -> tuple[float | None, OperatingProfitSource]:
    if summary is None:
        return None, OperatingProfitSource.NULL
    if summary.operating_profit is not None:
        return summary.operating_profit, OperatingProfitSource.OPERATING_PROFIT
    if summary.ordinary_profit is not None:
        return summary.ordinary_profit, OperatingProfitSource.ORDINARY_PROFIT
    if summary.profit is not None:
        return summary.profit, OperatingProfitSource.PROFIT
    return None, OperatingProfitSource.NULL


def _yoy_ratio(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current / previous) - 1.0


def _accruals_to_assets(
    *,
    eps_ttm: float | None,
    shares: float | None,
    ocf_ttm: float | None,
    total_assets: float | None,
    prior_total_assets: float | None,
) -> float | None:
    """Sloan (1996) accruals ratio: (NI - CFO) / average total assets.

    NI is approximated as ``eps_ttm * shares_outstanding``; if a true NI line
    becomes available later (J-Quants `profit` field), prefer it. The
    denominator uses the average of current and prior-year total assets when
    both are present, otherwise the current value. Returns None for any
    missing input or zero denominator.
    """
    if eps_ttm is None or shares is None or ocf_ttm is None or total_assets is None:
        return None
    net_income = eps_ttm * shares
    denominator = (
        (total_assets + prior_total_assets) / 2.0
        if prior_total_assets is not None and prior_total_assets > 0
        else total_assets
    )
    if denominator <= 0:
        return None
    return (net_income - ocf_ttm) / denominator


def _build_quality_signals(
    summaries: Sequence[JQuantsFinancialSummary],
    *,
    prior_year: JQuantsFinancialSummary | None,
    ttm_rules: TTMRules,
    total_assets: float | None,
    equity: float | None,
    accruals_to_assets: float | None,
    net_share_change_yoy: float | None,
) -> _QualitySignals:
    """Build eight unweighted, point-in-time quality conditions.

    The prior snapshot is rebuilt only from rows available through the matched
    prior-period disclosure. This keeps every delta on the same as-of boundary
    and prevents a later revision from leaking into the older side.
    """
    prior_summaries = _summaries_through(summaries, prior_year)
    profit_current, profit_prior = _common_ttm_pair(
        summaries,
        prior_summaries,
        fields=("operating_profit", "ordinary_profit", "profit"),
        ttm_rules=ttm_rules,
    )
    operating_current, operating_prior = _ttm_pair(
        summaries, prior_summaries, field="operating_profit", ttm_rules=ttm_rules
    )
    sales_current, sales_prior = _ttm_pair(
        summaries, prior_summaries, field="sales", ttm_rules=ttm_rules
    )
    cfo_current, _cfo_prior = _ttm_pair(
        summaries, prior_summaries, field="cfo", ttm_rules=ttm_rules
    )
    prior_total_assets, _ = _carry_forward(prior_summaries, "total_assets", prior_year)
    prior_equity, _ = _carry_forward(prior_summaries, "equity", prior_year)

    roa_current = _ratio_with_positive_denominator(profit_current, total_assets)
    roa_prior = _ratio_with_positive_denominator(profit_prior, prior_total_assets)
    operating_margin_current = _ratio_with_positive_denominator(operating_current, sales_current)
    operating_margin_prior = _ratio_with_positive_denominator(operating_prior, sales_prior)
    equity_ratio_current = _ratio_with_positive_denominator(equity, total_assets)
    equity_ratio_prior = _ratio_with_positive_denominator(prior_equity, prior_total_assets)
    asset_turnover_current = _ratio_with_positive_denominator(sales_current, total_assets)
    asset_turnover_prior = _ratio_with_positive_denominator(sales_prior, prior_total_assets)

    components = (
        _positive(roa_current),
        _increased(roa_current, roa_prior),
        _positive(cfo_current),
        None if accruals_to_assets is None else accruals_to_assets < 0,
        _increased(operating_margin_current, operating_margin_prior),
        _increased(equity_ratio_current, equity_ratio_prior),
        None if net_share_change_yoy is None else net_share_change_yoy <= 0,
        _increased(asset_turnover_current, asset_turnover_prior),
    )
    available_count, count = _quality_signal_counts(components)
    return _QualitySignals(*components, available_count=available_count, count=count)


def _summaries_through(
    summaries: Sequence[JQuantsFinancialSummary],
    target: JQuantsFinancialSummary | None,
) -> Sequence[JQuantsFinancialSummary]:
    if target is None:
        return ()
    for index in range(len(summaries) - 1, -1, -1):
        if summaries[index] is target:
            return summaries[: index + 1]
    return ()


def _ttm_pair(
    current_summaries: Sequence[JQuantsFinancialSummary],
    prior_summaries: Sequence[JQuantsFinancialSummary],
    *,
    field: str,
    ttm_rules: TTMRules,
) -> tuple[float | None, float | None]:
    current, _ = _ttm_value(current_summaries, field, ttm_rules)
    prior, _ = _ttm_value(prior_summaries, field, ttm_rules)
    return current, prior


def _common_ttm_pair(
    current_summaries: Sequence[JQuantsFinancialSummary],
    prior_summaries: Sequence[JQuantsFinancialSummary],
    *,
    fields: Sequence[str],
    ttm_rules: TTMRules,
) -> tuple[float | None, float | None]:
    for field in fields:
        current, prior = _ttm_pair(
            current_summaries, prior_summaries, field=field, ttm_rules=ttm_rules
        )
        if current is not None and prior is not None:
            return current, prior
    return None, None


def _ratio_with_positive_denominator(
    numerator: float | None, denominator: float | None
) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _positive(value: float | None) -> bool | None:
    return None if value is None else value > 0


def _increased(current: float | None, prior: float | None) -> bool | None:
    return None if current is None or prior is None else current > prior


def _quality_signal_counts(components: Sequence[bool | None]) -> tuple[int, int | None]:
    available_count = sum(value is not None for value in components)
    count = (
        sum(value is True for value in components)
        if available_count >= QUALITY_SIGNAL_MIN_AVAILABLE
        else None
    )
    return available_count, count


def _loss_narrowing(current: float | None, previous: float | None) -> bool | None:
    if current is None or previous is None:
        return None
    if current >= 0:
        return False
    return previous < 0 and current > previous


_TTM_QUALITY_FIELDS = tuple(
    field.name for field in fields(FinancialSnapshot) if field.name.startswith("ttm_quality_")
)


def _count_ttm_qualities(snapshots: Sequence[FinancialSnapshot]) -> dict[str, int]:
    # 集計対象は schema の ttm_quality_* field から導出する (次元を足したとき
    # ここの列挙更新漏れで集計 telemetry から欠落するのを防ぐ)。
    counts = {quality.value: 0 for quality in TTMQuality}
    for snapshot in snapshots:
        for field_name in _TTM_QUALITY_FIELDS:
            quality: TTMQuality = getattr(snapshot, field_name)
            counts[quality.value] += 1
    return counts


def _rank_to_percentiles(values: Mapping[str, float | None]) -> dict[str, float | None]:
    available = sorted((value, key) for key, value in values.items() if value is not None)
    if not available:
        return dict.fromkeys(values)
    total = len(available)
    output: dict[str, float | None] = dict.fromkeys(values)
    for index, (_, key) in enumerate(available, start=1):
        output[key] = index / total
    return output
