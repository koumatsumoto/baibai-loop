"""銘柄ごとの財務・派生指標。

Security Analysisの比較座標を産む。比や差を計算するときは、組み合わせる量の
次の4つの基準を一致させる。型と有限性だけでは基準の違いを検出できない。

1. **資本基準** — 発行済 (`shares_outstanding`) / 自己株控除後 (`_shares_excluding_treasury`)
   / 期中平均 (`average_shares`)。市場が値付けする量はすべて自己株控除後で組む
2. **株式基準** — 開示時点 / asof / 支払の基準日ごと。`_normalize_summaries_to_asof_basis`
   が per-share 値と株数を asof へ寄せるが、配当は支払ごとに基準が分かれる
3. **実体** — 連結 / 単体。EDINET の書類はどちらかの基準で読まれる
   (`_edinet_describes_same_entity`)
4. **期間** — 時点 / 期中累計 / TTM / 会社予想

基準の一致は推測でなく store 自身の冗長性で確かめられる。同じ量を別経路で出す値が
あり、実データの通期行で次が成り立つ (四半期行の `eps_ttm` は期中累計なので期間基準が
違い、全期間の行へ広げると 4 つの基準を混ぜた数になる)。

- 開示された自己資本比率 == `bps` x 自己株控除後株数 / `total_assets`
- 報告 `profit` == `eps_ttm` x `average_shares`
- EDINET の `total_assets` == 短信の `total_assets` (同じ連結基準)

派生する 2 つの規則:

- **per-share 値の和・差を作らない。** 各項が自分の期の株数で割られているので、株数が
  動いた会社では成立しない。合成は円で行い、1 株当たりへの換算は最後に 1 回だけ行う。
  per-share 同士の**比** (YoY) は分母の違いが希薄化を映すので正しい
- **per-share 値と株数を掛けて総額を作らない。** 掛ける株数はたいてい別の概念で、
  分子と分母が別の量になる。総額が欲しいなら報告された総額の行を読む
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, timedelta
from enum import Enum
from itertools import pairwise
from math import exp, isclose, isfinite, log, sqrt
from statistics import fmean, mean, median

from baibai_engine.market.bars import JQuantsAdjustmentFactorEvent, asof_basis_closes

from .margin_metrics import MarginBalance, margin_supply_demand
from .providers.edinet import EdinetMetricRecord
from .providers.jquants import (
    JQuantsDailyBar,
    JQuantsFinancialSummary,
)
from .rule_config import ScreeningRules, TTMRules, load_screening_rules
from .schema import (
    SECTOR_MEDIAN_BASIS_MARKET,
    SECTOR_MEDIAN_BASIS_SECTOR,
    UNRESOLVED_DIVIDEND_BASIS,
    DerivedMetrics,
    FinancialSnapshot,
    OperatingProfitSource,
    SecurityMaster,
    TTMQuality,
)

VALUATION_METRICS = ("per_forward", "per_trailing", "pbr", "ev_ebitda", "p_s")

# 業種中央値を自業種から出すのに要る母数。これを下回る業種は市場全体の中央値へ落ちる。
# 薄い標本の中央値は anchor として不安定なので落とす側を選ぶが、落ちた値は「業種との差」
# ではなく「市場との差」なので、`DerivedMetrics.sector_median_basis` に素性を残す。
MIN_SECTOR_MEDIAN_POPULATION = 10
# run と calibration が同名の valuation を異なる式で作らないための method identity。
# 式・資本分母・価格基準の意味を変える変更ではこの値を進め、旧 cache を再利用しない。
# 現行の方式: trailing 系も純資産倍率も円の総額で組み、価格側の量は自己株控除後の資本で
# 割る。純資産は普通株主に帰属する側を採り、円経路と 1 株当たり経路が食い違う会社では
# 後者を使う。株式基準は行ごとに決め、期末と開示日の間に権利落ちがある行は申告基準を
# 判定してから換算し、判定できない行は株数と per-share を答えない。
VALUATION_CALCULATION_REVISION = "capital-equity-action-basis-v20"

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

# 株主還元の変化と正規化PERは複数期の通期実績を必要とする。production の
# FinancialSnapshot / E[r] 入力窓は上の730日のまま維持し、正規化PERだけはFY行と
# split eventを疎に読む。全bar・全四半期を日次runへ載せないための別窓である。
SHAREHOLDER_RETURN_HISTORY_WINDOW_DAYS = 1200
NORMALIZED_EPS_HISTORY_WINDOW_DAYS = 2200

# 通期実績 DPS の accrual 期間を bound する暦日窓。前期の通期実績開示行が窓内に
# 無い (実績が 1 期分しか無い) ときのフォールバックで、開示日から約 1 年遡って
# その期間内・開示前の分割を carry へ反映するために使う。
DIVIDEND_ACCRUAL_LOOKBACK_DAYS = 400

# 5 年 carry へ反復させる会社予想の上限。直近実績の 2 倍を超える進行期予想は、
# 特別配当か未実現の還元転換かを機械入力から区別できない。実績が正なら既存の実績経路へ
# 倒し、実績 0 / 未観測は初配当を消さないため予想を使う。
_DIVIDEND_FORECAST_SPIKE_MULTIPLE = 2.0


@dataclass(frozen=True)
class MetricBuildResult:
    financials: Mapping[str, FinancialSnapshot]
    derived: Mapping[str, DerivedMetrics]
    ttm_quality_counts: Mapping[str, int]
    yoy_missing_count: int


@dataclass(frozen=True, slots=True)
class ShareholderReturnChangeSignals:
    """Point-in-time facts used only by the calibration panel."""

    dps_streak_up: bool | None
    dps_yoy_latest: float | None
    dps_guidance_up: bool | None
    dividend_initiation: bool | None
    share_count_reduction_streak: int | None
    shareholder_return_change: bool | None


@dataclass(frozen=True, slots=True)
class _CapitalBasisResolution:
    """時価総額に使える、同じ資本状態に属する発行済・自己株式数。"""

    issued: float | None
    treasury: float | None
    shares_ex_treasury: float | None
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class _NormalizedSummaryResult:
    """As-of-basis rows plus the newest point across which share facts cannot carry."""

    summaries: Sequence[JQuantsFinancialSummary]
    capital_basis_barrier: _AccountingObservationKey | None


@dataclass(frozen=True, slots=True, kw_only=True)
class NormalizedProfitSignals:
    """Split-safe multi-FY earnings inputs for Normalized Earnings Power and calibration."""

    normalized_per_3fy: float | None
    normalized_per_5fy: float | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProfitabilityLevelSignals:
    """PIT profitability levels used only by calibration panels."""

    operating_profit_to_assets: float | None
    operating_margin: float | None
    asset_turnover: float | None


_AccountingObservationKey = tuple[date, int, date, date, int, date]
_FISCAL_PERIOD_ORDER = {"1Q": 1, "2Q": 2, "3Q": 3, "FY": 4}


def _accounting_observation_key(
    summary: JQuantsFinancialSummary,
) -> _AccountingObservationKey:
    """Period-first order for actual facts and capital state.

    A delayed correction may be disclosed after the current quarter while describing
    an older period.  A valid completed-period end therefore precedes accounting labels
    and source revision order in this key.  A future reported period uses only its
    period-start lower bound; an inverted period has no chronology authority.  This
    preserves a lone future-labelled actual observation, prevents a malformed Q1 from
    replacing a valid Q2, and lets a completed short fiscal year outrank an earlier
    quarter whose nominal fiscal-year end happens to be later.
    """

    reported_end = summary.period_end or summary.fiscal_year_end
    completed = (
        reported_end
        if reported_end is not None
        and reported_end <= summary.disclosed_at
        and (summary.period_start is None or summary.period_start <= reported_end)
        else None
    )
    chronology = completed or (
        summary.period_start
        if reported_end is not None
        and reported_end > summary.disclosed_at
        and summary.period_start is not None
        else date.min
    )
    period_order = _FISCAL_PERIOD_ORDER.get(summary.fiscal_period or "", 0)
    return (
        chronology,
        int(completed is not None),
        summary.fiscal_year_end or date.min,
        summary.period_start or date.min,
        period_order,
        summary.disclosed_at,
    )


_ACTUAL_ANCHOR_FIELDS = (
    "sales",
    "cfo",
    "cash_eq",
    "total_assets",
    "equity",
    "operating_profit",
    "ordinary_profit",
    "profit",
    "eps_ttm",
    "bps",
    "shares_outstanding",
    "treasury_shares",
    "equity_to_asset_ratio",
    "dps_actual_annual",
    "average_shares",
)


def _actual_rows(
    summaries: Sequence[JQuantsFinancialSummary],
) -> list[JQuantsFinancialSummary]:
    return [
        summary
        for summary in summaries
        if any(getattr(summary, field_name) is not None for field_name in _ACTUAL_ANCHOR_FIELDS)
    ]


def _latest_actual_row(
    summaries: Sequence[JQuantsFinancialSummary],
    *,
    field_names: Sequence[str] = (),
) -> JQuantsFinancialSummary | None:
    required = tuple(field_names) or _ACTUAL_ANCHOR_FIELDS
    candidates = [
        summary
        for summary in summaries
        if (
            all(getattr(summary, field_name) is not None for field_name in required)
            if field_names
            else any(getattr(summary, field_name) is not None for field_name in required)
        )
    ]
    return max(candidates, key=_accounting_observation_key, default=None)


def _latest_actual_row_with_any(
    summaries: Sequence[JQuantsFinancialSummary],
    field_names: Sequence[str],
) -> JQuantsFinancialSummary | None:
    candidates = [
        summary
        for summary in summaries
        if any(getattr(summary, field_name) is not None for field_name in field_names)
    ]
    return max(candidates, key=_accounting_observation_key, default=None)


def _latest_actual_row_by_field_priority(
    summaries: Sequence[JQuantsFinancialSummary],
    field_names: Sequence[str],
) -> JQuantsFinancialSummary | None:
    """Pick the preferred non-null metric without leaving the latest accounting period.

    Actual corrections may update only a subset of a statement.  The newest row for a
    period therefore cannot make a more specific metric from an earlier revision of the
    same period disappear, but a metric from an older period must not replace a less
    specific metric observed in the current period.
    """

    latest = _latest_actual_row_with_any(summaries, field_names)
    if latest is None:
        return None
    period_key = _accounting_observation_key(latest)[:-1]
    for field_name in field_names:
        candidates = [
            summary
            for summary in summaries
            if _accounting_observation_key(summary)[:-1] == period_key
            and getattr(summary, field_name) is not None
        ]
        if candidates:
            return max(candidates, key=_accounting_observation_key)
    return None


def build_profitability_level_signals(
    summaries: Sequence[JQuantsFinancialSummary],
    asof_date: date,
    ttm_rules: TTMRules,
) -> ProfitabilityLevelSignals:
    """Build TTM profitability levels without profit-basis fallback or future rows."""

    available = tuple(
        summary
        for summary in sorted(summaries, key=lambda item: item.disclosed_at)
        if summary.disclosed_at <= asof_date
    )
    latest = _latest_summary(available)
    operating_profit_ttm, _ = _ttm_value(available, "operating_profit", ttm_rules)
    sales_ttm, _ = _ttm_value(available, "sales", ttm_rules)
    total_assets, _ = _carry_forward(available, "total_assets", latest)

    def ratio(numerator: float | None, denominator: float | None) -> float | None:
        if numerator is None or denominator is None or denominator <= 0:
            return None
        return numerator / denominator

    return ProfitabilityLevelSignals(
        operating_profit_to_assets=ratio(operating_profit_ttm, total_assets),
        operating_margin=ratio(operating_profit_ttm, sales_ttm),
        asset_turnover=ratio(sales_ttm, total_assets),
    )


def build_normalized_profit_signals(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    asof_date: date,
    *,
    close: float | None,
) -> NormalizedProfitSignals:
    """Build the preregistered 3/5-FY EPS anchors without filling missing years.

    Each anchor averages full-year per-share earnings across years, which is a mean of
    per-share values rather than a sum, so the years may carry different share counts.
    """
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

    return NormalizedProfitSignals(
        normalized_per_3fy=normalized_per(latest_three),
        normalized_per_5fy=normalized_per(latest_five),
    )


def build_metrics(
    asof_date: date,
    securities_by_ticker: Mapping[str, SecurityMaster],
    bars_by_ticker: Mapping[str, Sequence[JQuantsDailyBar]],
    summaries_by_ticker: Mapping[str, Sequence[JQuantsFinancialSummary]],
    edinet_by_ticker: Mapping[str, EdinetMetricRecord],
    rules: ScreeningRules | None = None,
    median_population: frozenset[str] | None = None,
    margin_latest: Mapping[str, MarginBalance] | None = None,
    margin_prior_26w: Mapping[str, MarginBalance] | None = None,
    valuation_history_sessions: int = VALUATION_HISTORY_SESSIONS,
    adjustment_events_by_ticker: Mapping[
        str, Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar]
    ]
    | None = None,
) -> MetricBuildResult:
    """Build per-ticker financial and derived metrics for the screen scope.

    ``margin_latest`` / ``margin_prior_26w`` carry the margin balances that were
    already published at ``asof_date``; leaving them out yields the same metrics
    with the supply/demand axes unset, which is what a store without a published
    margin balance produces.

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
        ticker_bars = bars_by_ticker.get(ticker, ())
        adjustment_events = (
            adjustment_events_by_ticker.get(ticker, ())
            if adjustment_events_by_ticker is not None
            else ticker_bars
        )
        latest_price = asof_basis_closes([latest_bar], adjustment_events, asof_date=asof_date)[0]
        latest_prices[ticker] = latest_price
        # bar は `_latest_bar_on_or_before` が asof で切る。開示行も同じ場所で切る。
        # 較正リプレイは過去の断面を作り直すので、asof より後の開示が 1 行混ざると
        # 「発表前の決算で割安に見える」行ができ、測ったすべての予測力が偽になる。
        # 呼び出し側が窓で切っている前提を置かない (本 module の他の 3 つの入口も
        # 同じ規律で自分で切っている)。
        normalization = _normalize_summaries_with_status(
            [
                summary
                for summary in summaries_by_ticker.get(ticker, ())
                if summary.disclosed_at <= asof_date
            ],
            adjustment_events,
            asof_date,
        )
        financials[ticker] = _build_financial_snapshot(
            latest_price=latest_price,
            summaries=normalization.summaries,
            edinet=edinet_by_ticker.get(ticker),
            rules=rules,
            ticker_bars=ticker_bars,
            adjustment_events=adjustment_events,
            capital_basis_barrier=normalization.capital_basis_barrier,
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
        ticker_bars = bars_by_ticker.get(ticker, ())
        adjustment_events = (
            adjustment_events_by_ticker.get(ticker, ())
            if adjustment_events_by_ticker is not None
            else ticker_bars
        )
        four_week = _price_change(
            ticker_bars,
            20,
            asof_date,
            adjustment_events=adjustment_events,
        )
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
        adjustment_events = (
            adjustment_events_by_ticker.get(ticker, ())
            if adjustment_events_by_ticker is not None
            else ticker_bars
        )
        valuation_history = _valuation_history(
            latest_prices[ticker],
            ticker_bars,
            snapshot,
            asof_date,
            adjustment_events=adjustment_events,
            history_sessions=valuation_history_sessions,
        )
        sector_gaps: dict[str, float | None] = {}
        sector_medians: dict[str, float | None] = {}
        sector_bases: dict[str, str] = {}
        self_percentiles: dict[str, float | None] = {}
        self_medians: dict[str, float | None] = {}
        sigma_gaps: dict[str, float | None] = {}
        for metric in VALUATION_METRICS:
            current = getattr(snapshot, metric)
            sector_values = sector_metric_values.get(sector, {}).get(metric, [])
            on_sector = len(sector_values) >= MIN_SECTOR_MEDIAN_POPULATION
            baseline = sector_values if on_sector else market_metric_values.get(metric, [])
            sector_median = median(baseline) if baseline else None
            sector_medians[metric] = sector_median
            # どちらの母集団が答えたかを値と同じ粒度で残す。両者は同じ語で呼ばれるが
            # 別の量で、薄い業種は市場より低倍率へ寄るため、素性が無いと gap の符号を
            # 業種の割安と読むか業種構成と読むかを後から分けられない。
            # 中央値そのものが出なかった軸には基準が無い。どちらも答えていないのに
            # 「市場へ落ちた」と書くと、EDINET 由来の軸のように母集団全体で値が立たない
            # 軸が全行 fallback として並び、実際に落ちた軸と見分けが付かなくなる。
            if sector_median is not None:
                sector_bases[metric] = (
                    SECTOR_MEDIAN_BASIS_SECTOR if on_sector else SECTOR_MEDIAN_BASIS_MARKET
                )
            sector_gaps[metric] = (
                ((current / sector_median) - 1.0)
                if current is not None and sector_median not in (None, 0)
                else None
            )
            history_values = valuation_history.get(metric, [])
            self_percentiles[metric] = _self_range_percentile(history_values, current)
            # 自己レンジの中央値。機械 E[r] の保守側 anchor に使う。標本が薄い履歴
            # (直近上場等) の中央値は anchor として不安定なため 100 本を下限にする。
            # `_valuation_history` は fundamentals を最新値で固定して価格だけを動かすので、
            # これは倍率の履歴ではなく価格の履歴を倍率の単位で表したものである。価格比例の
            # 軸では `自己中央値 / 現値` が軸によらず `median(終値) / 現値` に一致する。
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
            sector_median_basis=sector_bases,
            self_range_percentile=self_percentiles,
            self_range_median=self_medians,
            price_change_1d=_price_change(
                ticker_bars, 1, asof_date, adjustment_events=adjustment_events
            ),
            price_change_5d=_price_change(
                ticker_bars, 5, asof_date, adjustment_events=adjustment_events
            ),
            price_change_20d=_price_change(
                ticker_bars, 20, asof_date, adjustment_events=adjustment_events
            ),
            price_change_60d=_price_change(
                ticker_bars, 60, asof_date, adjustment_events=adjustment_events
            ),
            realized_volatility_60d=_realized_volatility(
                ticker_bars, 60, asof_date, adjustment_events=adjustment_events
            ),
            gap_from_52w_low=_gap_from_low(
                ticker_bars, 252, asof_date, adjustment_events=adjustment_events
            ),
            turnover_spike_5d=_turnover_spike(ticker_bars, asof_date),
            sigma_gap=sigma_gaps,
            sector_relative_strength_4w=sector_rs.get(sector),
            sector_relative_strength_percentile=rs_percentiles.get(sector),
            ticker_return_4w=ticker_returns_4w.get(ticker),
            sector_return_4w=mean(sector_returns[sector]) if sector in sector_returns else None,
            short_history_flag=listing_span_days < PRICE_HISTORY_WINDOW_DAYS,
            split_adjustment_flag=_has_split_adjustment_within_sessions(
                ticker_bars,
                asof_date,
                60,
                adjustment_events=adjustment_events,
            ),
            **asdict(
                margin_supply_demand(
                    latest=(margin_latest or {}).get(ticker),
                    prior_26w=(margin_prior_26w or {}).get(ticker),
                    avg_daily_volume_shares=_avg_daily_volume(ticker_bars, asof_date),
                    shares_outstanding=snapshot.shares_outstanding,
                    split_within_adv_window=_has_split_adjustment_within_sessions(
                        ticker_bars,
                        asof_date,
                        AVG_VOLUME_SESSIONS,
                        adjustment_events=adjustment_events,
                    ),
                    split_within_delta_window=_has_split_adjustment_within_sessions(
                        ticker_bars,
                        asof_date,
                        MARGIN_DELTA_SESSIONS,
                        adjustment_events=adjustment_events,
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
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    after: date,
    asof_date: date,
    *,
    include_boundary_day: bool = False,
) -> float:
    """`after` より後・asof 以前の bar の adjustment_factor の累積を返す。

    `include_boundary_day` は境界日当日の権利落ちも数える。財務開示行が申告する株式基準は
    期末であって開示日ではないので、開示日当日に権利落ちがあった行は分割前基準のまま公表
    される (store 全数で該当 3 行、いずれも直前開示行と同じ株数=分割前基準。分割後基準の
    例は無い)。開示行の正規化はこれを数える。期末と開示日の間に権利落ちがある行は逆に
    134 行中 103 行が既に分割後基準なので、境界を期末側へ広げると大半を二重換算する。
    """
    factor = 1.0
    for bar in ticker_bars:
        before_start = bar.traded_at < after if include_boundary_day else bar.traded_at <= after
        if before_start or bar.traded_at > asof_date:
            continue
        if bar.adjustment_factor in (None, 0.0, 1.0):
            continue
        assert bar.adjustment_factor is not None
        factor *= bar.adjustment_factor
    return factor


class _ShareBasis(Enum):
    """開示行が申告している株式基準。

    期末と開示日の間に権利落ちがある行は、提出者によって期末基準のままのものと分割を
    遡及適用したものに割れる。どちらかで換算の要否が逆になるので、行ごとに決める。
    """

    AS_OF_PERIOD_END = "as_of_period_end"
    AS_OF_DISCLOSURE = "as_of_disclosure"
    INDETERMINATE = "indeterminate"


def _closer_share_basis(ratio: float, interim: float) -> _ShareBasis:
    """株数比を、分割前基準と分割後基準のどちらに寄せるか。

    2 つの仮説は `1/interim` 倍 (実データで 1.3〜10 倍) 離れており、その間に乗るのは
    実際の増減資である。増減資は普通この間隔よりずっと小さいので、**対数空間で近い方の
    極へ寄せ、どちらの極からも幾何中点より遠いときだけ答えない**。固定幅の帯は極の間隔を
    無視するため、分割と同時に数 % の増資があった行 (7066 は比 2.048 / 期待 2.0) を
    分けられなくなる。
    """

    if ratio <= 0 or interim <= 0:
        return _ShareBasis.INDETERMINATE
    separation = abs(log(1.0 / interim))
    if separation == 0.0:
        return _ShareBasis.INDETERMINATE
    to_period_end = abs(log(ratio))
    to_disclosure = abs(log(ratio * interim))
    nearest = min(to_period_end, to_disclosure)
    # 幾何中点より遠い比は、どちらの極から見ても説明が付かない。分割が小さいほど極が
    # 近いので、この条件だけが効く場面がある (1.3 倍の分割では残差 14% で中点に届く)。
    if nearest > separation / 2.0:
        return _ShareBasis.INDETERMINATE
    # 極に寄せたあとに残る株数変化。実データではここが 12.4% までと 29.3% からに分かれ、
    # 間の 17pt は空である。空白の中央で切り、残差の大きい行は答えない — 分割と同時に
    # 大きな増減資があった行は、どちらの仮説を採っても株数が数倍ずれうる (6628 は
    # 1:5 併合と増資が重なり、比 0.51 が対数上は分割前基準に近く見える)。
    if abs(exp(-nearest) - 1.0) > _SPLIT_RESIDUAL_SHARE_CHANGE_LIMIT:
        return _ShareBasis.INDETERMINATE
    return (
        _ShareBasis.AS_OF_PERIOD_END
        if to_period_end <= to_disclosure
        else _ShareBasis.AS_OF_DISCLOSURE
    )


# 極へ寄せたあとに残ってよい株数変化。実測の空白 (12.4% / 29.3%) の中央に置く。
_SPLIT_RESIDUAL_SHARE_CHANGE_LIMIT = 0.20


def _interim_split_basis(
    summaries: Sequence[JQuantsFinancialSummary],
    index: int,
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
) -> tuple[float, _ShareBasis]:
    """期末と開示日の間の権利落ちの累積と、その行が申告している株式基準。

    権利落ちが無ければ換算は 1.0 で、基準を問う必要も無い。あるときは **権利落ち日以前に
    開示された最新行** の as-reported 株数と比べる。その行は分割より前に公表されているので
    必ず分割前基準にある。比が 1 ならこの行も分割前基準 (=期末基準) のまま公表されており、
    `1/factor` なら提出者が分割を遡及適用している。

    参照を「直前の行」にすると、訂正開示のように同じ値を持つ行が並んだ場合に基準の未確定な
    行と比べることになり、比 1.0 が「前行と同じ」ではなく「基準が同じ」と読めてしまう。
    """

    summary = summaries[index]
    period_end = summary.period_end
    if period_end is None:
        return 1.0, _ShareBasis.AS_OF_DISCLOSURE
    interim = 1.0
    first_ex: date | None = None
    for bar in ticker_bars:
        if not (period_end < bar.traded_at < summary.disclosed_at):
            continue
        if bar.adjustment_factor in (None, 0.0, 1.0):
            continue
        assert bar.adjustment_factor is not None
        interim *= bar.adjustment_factor
        if first_ex is None or bar.traded_at < first_ex:
            first_ex = bar.traded_at
    if interim in (0.0, 1.0) or first_ex is None:
        return 1.0, _ShareBasis.AS_OF_DISCLOSURE

    def classify(field_name: str) -> _ShareBasis:
        current = getattr(summary, field_name)
        previous_row = next(
            (
                row
                for row in reversed(summaries[:index])
                if (value := getattr(row, field_name)) is not None
                and value > 0
                and row.disclosed_at < first_ex
            ),
            None,
        )
        if current is None or current <= 0 or previous_row is None:
            return _ShareBasis.INDETERMINATE
        previous = getattr(previous_row, field_name)
        assert previous is not None
        # 比較元の開示後から現在行の期末までに別の corporate action がある場合、生の株数は
        # 現在行の期末基準ではない。先に同じ基準へ移さないと、連続した分割・併合をすべて
        # 今回の interim action の差と誤認する。開示日当日の権利落ちは開示行の正規化と同じ
        # 契約で数える。
        previous_to_period_end = _cumulative_adjustment_factor_after(
            ticker_bars,
            previous_row.disclosed_at,
            period_end,
            include_boundary_day=True,
        )
        if previous_to_period_end <= 0:
            return _ShareBasis.INDETERMINATE
        previous_on_period_end_basis = previous / previous_to_period_end
        return _closer_share_basis(current / previous_on_period_end_basis, interim)

    issued_basis = classify("shares_outstanding")
    if issued_basis is not _ShareBasis.INDETERMINATE:
        return interim, issued_basis
    # AvgSh は期中平均でありgross issuedの値としては使えないが、同じ開示のper-share値と
    # どちらのsplit basisを共有するかを判定する独立anchorにはなる。確定できる場合だけ
    # classifierを補い、正規化後のcapital値は引き続きShOutFY/TrShFYから作る。
    return interim, classify("average_shares")


def _without_share_basis(summary: JQuantsFinancialSummary) -> JQuantsFinancialSummary:
    """株式基準を決められなかった行から、基準に依存する量を落とす。

    円の総額 (総資産・売上・利益) は基準に依存しないので残す。株数と per-share は
    どちらの基準か分からないまま価格と組むと時価総額が分割比だけずれるので答えない。
    時価総額が出ない銘柄は母集団に入らない。
    """

    return replace(
        summary,
        eps_ttm=None,
        forecast_eps=None,
        bps=None,
        dps_actual_annual=None,
        dps_forecast_annual=None,
        shares_outstanding=None,
        average_shares=None,
        treasury_shares=None,
    )


def _normalize_summaries_to_asof_basis(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
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
    return _normalize_summaries_with_status(summaries, ticker_bars, asof_date).summaries


def _normalize_summaries_with_status(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    asof_date: date,
) -> _NormalizedSummaryResult:
    """Normalize rows while preserving an explicit fail-close capital carry barrier."""

    has_adjustment = any(
        bar.adjustment_factor not in (None, 0.0, 1.0)
        for bar in ticker_bars
        if bar.traded_at <= asof_date
    )
    if not has_adjustment:
        return _NormalizedSummaryResult(summaries=summaries, capital_basis_barrier=None)
    normalized: list[JQuantsFinancialSummary] = []
    capital_basis_barrier: _AccountingObservationKey | None = None
    for index, summary in enumerate(summaries):
        factor = _cumulative_adjustment_factor_after(
            ticker_bars, summary.disclosed_at, asof_date, include_boundary_day=True
        )
        interim, basis = _interim_split_basis(summaries, index, ticker_bars)
        if basis is _ShareBasis.INDETERMINATE:
            normalized.append(_without_share_basis(summary))
            observation_key = _accounting_observation_key(summary)
            if (summary.shares_outstanding is not None or summary.treasury_shares is not None) and (
                capital_basis_barrier is None or observation_key > capital_basis_barrier
            ):
                capital_basis_barrier = observation_key
            continue
        if basis is _ShareBasis.AS_OF_PERIOD_END:
            # 期末基準のまま公表された行なので、期末と開示日の間の権利落ちも数える。
            factor *= interim
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
                # 期中平均株式数も株数なので同じ換算を掛ける。掛けないと、開示より後に
                # 分割のある行で「発行済 - 自己株」との比が factor 倍ずれ、健全性 gate が
                # 本来効かせたい (分割が複数ある) 行でだけ静かに無効になる。
                average_shares=(
                    summary.average_shares / factor if summary.average_shares is not None else None
                ),
                # 自己株式数は発行済と同じ株数なので同じ換算を掛ける。片方だけ換算すると
                # 差である自己株控除後株式数が分割のたびに壊れる。`equity_to_asset_ratio`
                # は比率なので分割で動かず、そのまま持ち越される。
                treasury_shares=(
                    summary.treasury_shares / factor
                    if summary.treasury_shares is not None
                    else None
                ),
            )
        )
    return _NormalizedSummaryResult(
        summaries=normalized,
        capital_basis_barrier=capital_basis_barrier,
    )


def _latest_non_null_row(
    summaries: Sequence[JQuantsFinancialSummary], field_name: str
) -> JQuantsFinancialSummary | None:
    """指定 field を観測した最新行を返す。値だけでなく資本状態の出所行を保持する。"""

    return _latest_actual_row(summaries, field_names=(field_name,))


def _latest_complete_row(
    summaries: Sequence[JQuantsFinancialSummary], field_names: Sequence[str]
) -> JQuantsFinancialSummary | None:
    """指定した値を同時に観測した最新行を返す。"""

    return _latest_actual_row(summaries, field_names=field_names)


def _issued_matches_average_alias(
    summary: JQuantsFinancialSummary,
    treasury: float,
    summaries: Sequence[JQuantsFinancialSummary],
) -> bool:
    """Identify a stored AvgSh fallback only when an earlier gross issue count proves it."""

    issued = summary.shares_outstanding
    average = summary.average_shares
    if issued is None or average is None or treasury <= 0:
        return False
    if not isclose(issued, average, rel_tol=1e-12, abs_tol=1e-6):
        return False
    reconstructed_gross = issued + treasury
    source_key = _accounting_observation_key(summary)
    return any(
        row.shares_outstanding is not None
        and _accounting_observation_key(row) < source_key
        and isclose(
            reconstructed_gross,
            row.shares_outstanding,
            rel_tol=1e-12,
            abs_tol=1e-6,
        )
        for row in summaries
    )


def _resolve_capital_basis(
    summaries: Sequence[JQuantsFinancialSummary],
    *,
    capital_basis_barrier: _AccountingObservationKey | None = None,
) -> _CapitalBasisResolution:
    """発行済と自己株式を、両方が成立する資本状態でだけ組み合わせる。

    自己株式0株がJ-Quantsで欠損値になる行があるため、正の自己株式を観測した後に発行済株式を
    持つ新しい行が自己株式を欠く場合、旧自己株式が現在状態でも有効か判別できない。
    自己株式の消却、処分、新株発行と処分の同時実施を区別できないので、新しい自己株式を
    観測するまで旧正値をcarryせずfail closedにする。自己株式0株は二重控除を起こさない
    ため、明示的な0観測は通常どおりcarryできる。

    `AvgSh` は EPS の期中平均株式数であり、発行済株式総数ではない。発行済と期中平均が
    同値で、さらに自己株式を足すと過去に観測した gross issued へ戻る場合、入力の発行済欄へ
    期中平均がfallbackしたと識別できるので答えない。単なる同値は1Qの正常行にもあるため
    failureにはしない。ingestも`ShOutFY`だけを発行済株式総数として保存する。
    """

    actual_rows = _actual_rows(summaries)
    capital_rows = (
        [row for row in actual_rows if _accounting_observation_key(row) > capital_basis_barrier]
        if capital_basis_barrier is not None
        else actual_rows
    )
    issued_row = _latest_non_null_row(capital_rows, "shares_outstanding")
    treasury_row = _latest_non_null_row(capital_rows, "treasury_shares")
    issued = issued_row.shares_outstanding if issued_row is not None else None
    treasury = treasury_row.treasury_shares if treasury_row is not None else None
    if issued is None or treasury is None:
        return _CapitalBasisResolution(
            issued=issued,
            treasury=treasury,
            shares_ex_treasury=None,
            failure_reason=(
                "indeterminate_share_basis" if capital_basis_barrier is not None else None
            ),
        )
    if issued <= 0 or treasury < 0 or issued <= treasury:
        return _CapitalBasisResolution(
            issued=issued,
            treasury=treasury,
            shares_ex_treasury=None,
            failure_reason="invalid_issued_or_treasury_shares",
        )

    if issued_row is not None and treasury_row is not None:
        issued_source_key = _accounting_observation_key(issued_row)
        treasury_source_key = _accounting_observation_key(treasury_row)
        if issued_source_key != treasury_source_key and treasury > 0:
            issued_at_treasury = treasury_row.shares_outstanding
            if issued_at_treasury is None:
                return _CapitalBasisResolution(
                    issued=issued,
                    treasury=treasury,
                    shares_ex_treasury=None,
                    failure_reason="treasury_observation_without_issued_basis",
                )
            if issued_at_treasury <= 0 or issued_at_treasury <= treasury:
                return _CapitalBasisResolution(
                    issued=issued,
                    treasury=treasury,
                    shares_ex_treasury=None,
                    failure_reason="invalid_treasury_source_capital_basis",
                )
            if _accounting_observation_key(issued_row) > _accounting_observation_key(treasury_row):
                return _CapitalBasisResolution(
                    issued=issued,
                    treasury=treasury,
                    shares_ex_treasury=None,
                    failure_reason=(
                        "indeterminate_positive_treasury_after_later_issued_observation"
                    ),
                )

        if _issued_matches_average_alias(issued_row, treasury, capital_rows):
            return _CapitalBasisResolution(
                issued=issued,
                treasury=treasury,
                shares_ex_treasury=None,
                failure_reason="issued_matches_average_with_positive_treasury",
            )

    return _CapitalBasisResolution(
        issued=issued,
        treasury=treasury,
        shares_ex_treasury=_shares_excluding_treasury(issued, treasury),
        failure_reason=None,
    )


def build_shares_outstanding_index(
    summaries_by_ticker: Mapping[str, Sequence[JQuantsFinancialSummary]],
    bars_by_ticker: Mapping[str, Sequence[JQuantsDailyBar]],
    asof_date: date,
    *,
    adjustment_events_by_ticker: Mapping[
        str, Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar]
    ]
    | None = None,
) -> dict[str, float | None]:
    """Shares the market can price: issued less treasury, on the as-of split basis.

    universe の時価総額はこの index から作られ、流動性 gate (100 億円) の分母になる。
    `FinancialSnapshot.market_cap` と同じ株数で作らないと、同じ「時価総額」という語が
    2 つの値を指す。株数と自己株式数は BS 系 fact なので四半期開示に載らないことが多く、
    直近の非 null 行から carry-forward する。ただし正の自己株式より後に発行済だけを
    再観測した状態や、期中平均株式数と区別できない発行済は同じ資本状態と確認できないため、
    時価総額を答えない。
    """
    shares: dict[str, float | None] = {}
    for ticker, summaries in summaries_by_ticker.items():
        available = [row for row in summaries if row.disclosed_at <= asof_date]
        ticker_bars = bars_by_ticker.get(ticker, ())
        adjustment_events = (
            adjustment_events_by_ticker.get(ticker, ())
            if adjustment_events_by_ticker is not None
            else ticker_bars
        )
        normalized = _normalize_summaries_with_status(available, adjustment_events, asof_date)
        shares[ticker] = _resolve_capital_basis(
            normalized.summaries,
            capital_basis_barrier=normalized.capital_basis_barrier,
        ).shares_ex_treasury
    return shares


def group_adjustment_events_by_ticker(
    events: Sequence[JQuantsAdjustmentFactorEvent],
) -> dict[str, list[JQuantsAdjustmentFactorEvent]]:
    grouped: dict[str, list[JQuantsAdjustmentFactorEvent]] = {}
    for event in events:
        grouped.setdefault(event.ticker, []).append(event)
    for ticker in grouped:
        grouped[ticker].sort(key=lambda item: item.traded_at)
    return grouped


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


@dataclass(frozen=True, slots=True)
class _EarningsForecast:
    """One target-period earnings forecast resolved from ordered source documents."""

    target_period_end: date
    source: JQuantsFinancialSummary
    eps: float | None
    profit: float | None
    ordinary_profit: float | None


@dataclass(frozen=True, slots=True)
class _DividendForecast:
    """One target-period annual dividend forecast and its source document."""

    target_period_end: date
    source: JQuantsFinancialSummary
    annual_dps: float | None


def _resolve_earnings_forecast(
    summaries: Sequence[JQuantsFinancialSummary],
    latest_actual: JQuantsFinancialSummary | None,
) -> _EarningsForecast | None:
    """Resolve the selected forecast under the persisted date-only v23 contract.

    The store does not retain document identity or current/next target metadata.  The
    newest persisted row is therefore the only state that both direct and SQLite paths
    can reproduce.  Period-aware composition needs a lossless source shape and is not
    inferred from the collapsed row.
    """

    del latest_actual
    source = summaries[-1] if summaries else None
    if source is None:
        return None
    return _EarningsForecast(
        target_period_end=date.max,
        source=source,
        eps=source.forecast_eps,
        profit=source.forecast_profit,
        ordinary_profit=source.forecast_ordinary_profit,
    )


def _resolve_dividend_forecast(
    summaries: Sequence[JQuantsFinancialSummary],
    latest_actual: JQuantsFinancialSummary | None,
) -> _DividendForecast | None:
    """Resolve annual DPS without carrying a forecast behind the latest actual DPS."""

    del latest_actual
    actual_dates = [row.disclosed_at for row in summaries if row.dps_actual_annual is not None]
    threshold = max(actual_dates, default=None)
    for source in sorted(summaries, key=lambda item: item.disclosed_at, reverse=True):
        if threshold is not None and source.disclosed_at < threshold:
            return None
        value = source.dps_forecast_annual
        if value is not None and value >= 0:
            return _DividendForecast(
                target_period_end=date.max,
                source=source,
                annual_dps=value,
            )
    return None


def _actual_dps_rows(
    summaries: Sequence[JQuantsFinancialSummary],
) -> list[JQuantsFinancialSummary]:
    return [
        summary
        for summary in sorted(summaries, key=_accounting_observation_key, reverse=True)
        if summary.dps_actual_annual is not None
    ]


# 配当の基準日と corporate action がこの日数以内に並ぶ年度は換算しない。日本の分割は
# 「権利落ち = 基準日の前営業日、効力発生 = 基準日の翌日」が定型なので、期末配当の基準日と
# 分割の権利落ち日が数日違いで並ぶ。store が持つのは権利落ち日だけで効力発生日を持たない
# ため、その配当が action の前の株数で払われたのか後なのかを言えない。実測では調整日は
# 基準日の 5 日以内に集中し、5〜20 日の帯には 1 件も現れない (#901)。
DIVIDEND_RECORD_DATE_GUARD_DAYS = 5
# 総額から出した 1 株当たりと、支払ごとに換算した合計が食い違ってよい幅。総額は百万円
# 単位で開示され、割る株数は期末時点なので、支払の基準日の株数とは自社株買いのぶんだけ
# ずれる。この幅を超える食い違いは、どちらかの経路が別の株式基準を見ている合図になる。
DIVIDEND_ROUTE_TOLERANCE = 0.05
# 期末発行済から自己株を引いた株数が、提出者自身が EPS を出すのに使った期中平均株数から
# この倍率を超えて外れる行は、株数を per-share の分母に使わない。
SHARE_COUNT_ANCHOR_TOLERANCE = 2.0

# EDINET の総資産が短信の総資産からこの倍率を超えて外れる行は、同じ会社の貸借対照表では
# ないとみなし、EDINET 由来の値を判断面へ出さない。
ENTITY_SCALE_TOLERANCE = 2.0
CONSOLIDATED_BASIS = "consolidated"
ENTITY_SCALE_MISMATCH = "entity_scale_mismatch"

# 1 株当たりで開示される field。期をまたぐ和・差を作れないので TTM 合成へ渡さない。
_PER_SHARE_FIELDS = frozenset(
    {
        "eps_ttm",
        "forecast_eps",
        "bps",
        "dps_actual_annual",
        "dps_forecast_annual",
        "dividend_q1",
        "dividend_interim",
        "dividend_q3",
        "dividend_year_end",
    }
)


def _dividend_accrual_start(row: JQuantsFinancialSummary) -> date:
    """その年度の配当が積み上がり始めた日。

    当期会計期間の開始日を使う。前期の通期実績開示日を起点にすると、同一年度の訂正開示が
    直前に来た行で窓が数日へ縮み、窓内の corporate action が消える。
    """
    if row.period_start is not None:
        return row.period_start
    if row.fiscal_year_end is not None:
        return _shift_months(row.fiscal_year_end, -12)
    return row.disclosed_at - timedelta(days=DIVIDEND_ACCRUAL_LOOKBACK_DAYS)


def _dividend_basis_factor(
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    *,
    accrual_start: date,
    disclosed_at: date,
) -> float:
    """会計期間に起きた分割・併合の累積 factor。1.0 なら年度を通じて基準が一意。

    年間 DPS は中間・期末それぞれの基準日時点の株式基準で記載され、開示日で基準が決まる
    わけではない。期間内に分割・併合が入ると支払ごとに action の前後が分かれるので、
    報告された年間値をそのまま株価と比べられない。
    """
    return _cumulative_adjustment_factor_after(ticker_bars, accrual_start, disclosed_at)


def _dividend_record_dates(row: JQuantsFinancialSummary) -> tuple[tuple[float | None, date], ...]:
    """支払ごとの 1 株当たり配当と、その基準日。基準日は四半期末に置く。"""
    fiscal_year_end = row.fiscal_year_end
    if fiscal_year_end is None:
        return ()
    return (
        (row.dividend_q1, _shift_months(fiscal_year_end, -9)),
        (row.dividend_interim, _shift_months(fiscal_year_end, -6)),
        (row.dividend_q3, _shift_months(fiscal_year_end, -3)),
        (row.dividend_year_end, fiscal_year_end),
    )


def _shares_for_per_share(row: JQuantsFinancialSummary) -> float | None:
    """円の総額を 1 株当たりへ直すのに使える株数。壊れている行は答えない。"""
    shares = _shares_excluding_treasury(row.shares_outstanding, row.treasury_shares)
    if shares is None:
        return None
    anchor = row.average_shares
    if anchor is None or anchor <= 0:
        return shares
    ratio = shares / anchor
    if not 1 / SHARE_COUNT_ANCHOR_TOLERANCE <= ratio <= SHARE_COUNT_ANCHOR_TOLERANCE:
        return None
    return shares


def _asof_basis_dividend(
    row: JQuantsFinancialSummary,
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    *,
    asof_date: date,
) -> float | None:
    """分割・併合を跨いだ年度の年間配当を asof の株式基準で答える。答えられなければ None。

    支払ごとに、その基準日より後の調整だけを掛けて足す。この経路は期末発行済株式数を
    使わないので、提出者がその株数を遡及修正したかどうかに左右されない。基準日のすぐ
    そばに調整がある年度は、権利落ち日しか持たない store からは前後を決められないので
    答えない。答えられた値は 2 つの独立な量と突き合わせる。調整を掛ける前の明細合計が
    報告された年間値と一致すること (明細の欠落を「解決済み」として出さない)、そして
    株式基準を持たない配当総額から出した 1 株当たりと一致すること。
    """
    reported = row.dps_actual_annual
    if reported is None:
        return None
    # 無配の年度に確定すべき株式基準は無い。0 円は何倍しても 0 円なので、期間内に
    # 調整があっても答えは 0 で確定する。ここを拒否に倒すと、分割を出す無配銘柄が
    # 利回りだけでなく E[r] ごと判断面から消える。
    if reported == 0:
        return 0.0
    payments = _dividend_record_dates(row)
    if not payments or all(value is None for value, _ in payments):
        return None
    # 明細は feed の欠落で一部だけ来ることがある。調整前の合計が報告年間値と合わない行は
    # 真値の一部しか持っていないので、換算しても真値の一部にしかならない。
    #
    # 2 つの量は株式基準が違う。明細は開示されたままで、`dps_actual_annual` は
    # `_normalize_summaries_to_asof_basis` が asof 基準へ寄せている。同じ換算を明細側へ
    # 掛けてから比べる。境界も揃える — 片方だけが換算する、あるいは片方だけが開示日当日の
    # 権利落ちを数えると、比は必ず換算係数の逆数になり、その年度を「明細が欠けている」と
    # して捨てる。捨てた年度は増配判定ごと消える。
    detail_sum = sum(value or 0.0 for value, _ in payments) * _cumulative_adjustment_factor_after(
        ticker_bars, row.disclosed_at, asof_date, include_boundary_day=True
    )
    if abs(detail_sum / reported - 1.0) > DIVIDEND_ROUTE_TOLERANCE:
        return None
    window_start = _dividend_accrual_start(row)
    adjustments = [
        bar
        for bar in ticker_bars
        if window_start < bar.traded_at <= row.disclosed_at
        and bar.adjustment_factor not in (None, 0.0, 1.0)
    ]
    guard = timedelta(days=DIVIDEND_RECORD_DATE_GUARD_DAYS)
    for value, record_date in payments:
        if not value:
            continue
        if any(abs(bar.traded_at - record_date) <= guard for bar in adjustments):
            return None
    resolved = sum(
        (value or 0.0) * _cumulative_adjustment_factor_after(ticker_bars, record_date, asof_date)
        for value, record_date in payments
    )
    if resolved <= 0:
        return None
    amount = row.dividend_total_annual
    shares = _shares_for_per_share(row)
    if (
        amount is not None
        and amount > 0
        and shares is not None
        and abs((amount / shares) / resolved - 1.0) > DIVIDEND_ROUTE_TOLERANCE
    ):
        return None
    return resolved


def _resolve_dividend_carry(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    latest_price: float,
    asof_date: date,
) -> _DividendCarry:
    """carry 用の配当利回りと基準を解決する。

    実績 DPS は開示時点の株式基準で記載され、`_normalize_summaries_to_asof_basis` が
    開示日より後の分割を掛けて asof 基準へ寄せている。残るのは会計期間の中で起きた
    分割・併合で、報告された年間値は支払ごとに基準が分かれるためそのままでは株価と
    比べられない。その年度は支払ごとに換算し直し (`_asof_basis_dividend`)、換算できな
    ければ実績側の利回りを出さない。carry の配当側は予想 DPS または基準を解決できた実績
    DPS だけで組む。予想 DPS は分割を跨ぐ行で正規化が None へ落としているので、同じ規律が
    既に効いている。

    予想は実績より優先するが、優先できるのは実績より新しく、かつ正の実績 DPS の 2 倍
    以下のときだけである。2 倍を超える跳ねは特別配当か未実現の還元転換かを機械入力から
    区別できないので、5 年反復する carry には実績を使う。会社が予想を取り下げた後も過去の
    予想を引き当て続けると、無配化した会社に当時の配当額の利回りが付き、E[r] の reversion
    上限 (5%/年) を単独で超える carry を作る。
    """
    actual_rows = _actual_dps_rows(summaries)
    latest_actual = _latest_summary(summaries)
    forecast_state = _resolve_dividend_forecast(summaries, latest_actual)
    forecast = forecast_state.annual_dps if forecast_state is not None else None
    basis_factor = 1.0
    actual_annual: float | None = None
    if actual_rows:
        latest_actual = actual_rows[0]
        assert latest_actual.dps_actual_annual is not None
        basis_factor = _dividend_basis_factor(
            ticker_bars,
            accrual_start=_dividend_accrual_start(latest_actual),
            disclosed_at=latest_actual.disclosed_at,
        )
        if basis_factor == 1.0:
            actual_annual = latest_actual.dps_actual_annual
        else:
            actual_annual = _asof_basis_dividend(latest_actual, ticker_bars, asof_date=asof_date)

    recorded_factor = basis_factor if basis_factor != 1.0 else None

    forecast_is_usable_for_carry = (
        forecast is not None
        and forecast > 0
        and (
            actual_annual is None
            or actual_annual <= 0
            or forecast <= _DIVIDEND_FORECAST_SPIKE_MULTIPLE * actual_annual
        )
    )

    if latest_price <= 0:
        basis = "unavailable"
    elif forecast_is_usable_for_carry and forecast is not None:
        return _DividendCarry(
            dividend_yield=forecast / latest_price,
            dps_actual_annual=actual_annual,
            dps_forecast_annual=forecast,
            basis="forecast_annual",
            split_factor=recorded_factor,
        )
    elif actual_annual is not None and actual_annual > 0:
        return _DividendCarry(
            dividend_yield=actual_annual / latest_price,
            dps_actual_annual=actual_annual,
            dps_forecast_annual=forecast,
            basis="actual_reported" if recorded_factor is None else "actual_record_date_resolved",
            split_factor=recorded_factor,
        )
    elif recorded_factor is not None and actual_annual is None:
        # 解決できなかった年度だけを拒否にする。解決できて 0 だった年度 (無配) は、
        # 出す利回りが無いという点で観測できない年度と同じ扱いでよく、拒否にすると
        # 分割を出す無配銘柄が E[r] ごと判断面から消える。
        basis = UNRESOLVED_DIVIDEND_BASIS
    else:
        basis = "unavailable"

    return _DividendCarry(
        dividend_yield=None,
        dps_actual_annual=actual_annual,
        dps_forecast_annual=forecast,
        basis=basis,
        split_factor=recorded_factor,
    )


def build_shareholder_return_change_signals(
    summaries: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
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

    dps_values = _asof_basis_fy_dps(fy_rows, ticker_bars, asof_date=asof_date)
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
    forecast_state = _resolve_dividend_forecast(normalized, _latest_summary(normalized))
    forecast = forecast_state.annual_dps if forecast_state is not None else None
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
        and not (
            row.treasury_shares is not None
            and _issued_matches_average_alias(row, row.treasury_shares, normalized)
        )
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


def _asof_basis_fy_dps(
    fy_rows: Sequence[JQuantsFinancialSummary],
    ticker_bars: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    *,
    asof_date: date,
) -> dict[date, float]:
    """通期実績 DPS を fiscal year end で引けるようにする。

    行は `_normalize_summaries_to_asof_basis` を通っているので asof 基準に揃っている。
    会計期間に分割・併合が入った年度だけは報告値の基準が支払ごとに分かれるので、支払
    ごとに換算し直す。換算できない年度は落とし、落ちた年度を含む YoY と streak は
    `_latest_consecutive_values` が None を返して増配と減配を取り違えない。
    """
    values: dict[date, float] = {}
    for row in fy_rows:
        fiscal_year_end = row.fiscal_year_end
        value = row.dps_actual_annual
        if fiscal_year_end is None or value is None or value < 0:
            continue
        factor = _dividend_basis_factor(
            ticker_bars,
            accrual_start=_dividend_accrual_start(row),
            disclosed_at=row.disclosed_at,
        )
        if factor == 1.0:
            values[fiscal_year_end] = value
            continue
        resolved = _asof_basis_dividend(row, ticker_bars, asof_date=asof_date)
        if resolved is not None:
            values[fiscal_year_end] = resolved
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
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
    capital_basis_barrier: _AccountingObservationKey | None,
    asof_date: date,
) -> FinancialSnapshot:
    latest = _latest_summary(summaries)
    forecast = _resolve_earnings_forecast(summaries, latest)
    forecast_eps = forecast.eps if forecast is not None else None
    # 会社予想で純利益>経常なら特別益をほぼ確定する 1 行チェック (税負担が通常正)。
    # 純利益/経常は forecast_eps と同一予想期のペアで ingest 済み・分割不変の絶対額なので、
    # 両方揃うときだけ比較する。flag は warning で per_forward / E[r] / rank を変えない。
    forecast_profit = forecast.profit if forecast is not None else None
    forecast_ordinary_profit = forecast.ordinary_profit if forecast is not None else None
    forecast_special_gain_flag = (
        forecast_profit is not None
        and forecast_ordinary_profit is not None
        and forecast_profit > forecast_ordinary_profit
    )
    # 会社予想の通期赤字。片方しか開示されない期があるので and でなく or で見る。予想が
    # 1 つも無い行は「黒字予想」ではないので False のまま置く (欠損と黒字を畳まない)。
    # 赤字予想は forecast EPS を負にして forward PER を落とし、FV アンカーを自己履歴 PBR
    # だけにする。その倍率は黒字だった時代のものなので、判断前に事実として出す。
    forecast_full_year_loss_flag = (forecast_profit is not None and forecast_profit < 0) or (
        forecast_ordinary_profit is not None and forecast_ordinary_profit < 0
    )
    # 開示の利益は期中累計で、年度途中の四半期開示では 12 か月分にならない (Q1 開示だと
    # 3 か月分)。sales / cfo と同じ rolling 合成 (直近累計 + 前期通期 - 前年同期間累計) で
    # TTM に直し、通期開示のときだけそのまま使う。合成できない場合は per_trailing を
    # 出さない (単一四半期の利益で割った偽の割高 PER を作らない)。
    # 合成は 1 株当たりでなく円で行う。1 株当たりの各項は自分の期の株数で割られており、
    # 株数が動いた会社では和・差が成立しない (新株発行で株数が倍になった期を跨ぐと、
    # 黒字の会社が赤字に見える)。1 株当たりへの換算は最後に 1 回だけ行う。
    eps_row = _latest_actual_row(summaries, field_names=("eps_ttm",))
    eps_prior = _prior_year_summary(summaries, rules.ttm, field_name="eps_ttm")
    sales_row = _latest_actual_row(summaries, field_names=("sales",))
    sales_prior = _prior_year_summary(summaries, rules.ttm, field_name="sales")
    cfo_row = _latest_actual_row(summaries, field_names=("cfo",))
    cfo_prior = _prior_year_summary(summaries, rules.ttm, field_name="cfo")
    eps_cumulative = eps_row.eps_ttm if eps_row else None
    profit_ttm, profit_quality = _ttm_value(summaries, "profit", rules.ttm)
    # BS 系 fact (bps / cash_eq / equity / total_assets / 株数) は四半期開示に
    # 載らないことが多く (bps 非 null は FY 開示 ~69% に対し四半期 ~17-20%)、
    # latest 行だけを見ると四半期行が最新になる断面で PBR 等が季節的に大量欠損
    # する。直近の非 null 行から carry-forward し (値は asof-basis 正規化済み)、
    # どの field をいつの開示から引いたかを staleness fact として残す。

    cash_eq, cash_eq_lag = _carry_forward(summaries, "cash_eq", latest)
    total_assets, total_assets_lag = _carry_forward(summaries, "total_assets", latest)
    equity_to_asset_ratio, eq_ratio_lag = _carry_forward(summaries, "equity_to_asset_ratio", latest)
    bps, bps_lag = _carry_forward(summaries, "bps", latest)
    carried_lags = {
        "bps": bps_lag,
        "cash_eq": cash_eq_lag,
        "total_assets": total_assets_lag,
        "equity_to_asset_ratio": eq_ratio_lag,
    }
    bs_carry_forward_fields = ",".join(
        sorted(name for name, lag in carried_lags.items() if lag is not None and lag > 0)
    )
    bs_carry_forward_lag_days = max(
        (lag for lag in carried_lags.values() if lag is not None), default=None
    )
    # carry 用の配当利回りは予想 DPS を優先する。ただし正の実績 DPS の 2 倍を超える
    # 跳ねは 5 年反復させず、実績へ倒す。予想を使えなければ accrual 期間の分割 factor で
    # 調整した実績 DPS を使う。実績 DPS は
    # FY 開示にしか載らないため直近の非 null 行から取り、split-safe 化した値を
    # snapshot の実績 DPS として記録する (株価と同じ分割後基準で表示・比較できる)。
    dividend = _resolve_dividend_carry(summaries, adjustment_events, latest_price, asof_date)
    dps_actual_annual = dividend.dps_actual_annual
    dps_forecast_annual = dividend.dps_forecast_annual
    dividend_yield = dividend.dividend_yield
    per_forward = (latest_price / forecast_eps) if forecast_eps and forecast_eps > 0 else None
    operating_row = _latest_actual_row_by_field_priority(
        summaries,
        ("operating_profit", "ordinary_profit", "profit"),
    )
    operating_profit, operating_profit_source = _select_operating_profit(operating_row)
    operating_field = {
        OperatingProfitSource.OPERATING_PROFIT: "operating_profit",
        OperatingProfitSource.ORDINARY_PROFIT: "ordinary_profit",
        OperatingProfitSource.PROFIT: "profit",
    }.get(operating_profit_source)
    operating_prior_row = (
        _prior_year_summary(summaries, rules.ttm, field_name=operating_field)
        if operating_field is not None
        else None
    )
    operating_profit_prior_year, _ = _select_operating_profit(operating_prior_row)
    capital_basis = _resolve_capital_basis(
        summaries,
        capital_basis_barrier=capital_basis_barrier,
    )
    shares_outstanding = capital_basis.issued
    # 時価総額の分母は自己株式を除いた株数である。自己株式は議決権も配当請求権も持たない
    # ので、含めると時価総額が過大になり現金比率・利回りが薄く、倍率が割高に見える。歪みが
    # 最大になるのは自己株式を積み上げた企業、つまり buyback を実行した企業で、carry が
    # 上位へ押し上げる群と重なる。自己株式数が観測できない行は時価総額を出さない — 発行済で
    # 代用すると、どれだけ過大かが分からない値が現金比率・利回り・流動性 gate へ入る。
    shares_ex_treasury = capital_basis.shares_ex_treasury
    sales_ttm, sales_quality = _ttm_value(summaries, "sales", rules.ttm)
    ocf_ttm, ocf_quality = _ttm_value(summaries, "cfo", rules.ttm)
    # EDINET の値は 1 つの書類を連結・単体のどちらかの基準で読んだもので、時価総額と
    # TTM 系列は短信由来である。連結財務諸表を持つ会社の書類を単体基準で読むと、比率の
    # 分子と分母が別の会社を指す。両側が総資産を持つので実体の一致は直接確かめられる。
    entity_matches = _edinet_describes_same_entity(edinet, total_assets)
    edinet_metrics = edinet if entity_matches else None
    edinet_ocf_ttm = edinet_metrics.ocf_ttm if edinet_metrics else None
    debt = edinet_metrics.debt if edinet_metrics else None
    cash = edinet_metrics.cash if edinet_metrics else None
    ebitda_ttm = edinet_metrics.ebitda_ttm if edinet_metrics else None
    fcf_ttm = edinet_metrics.fcf_ttm if edinet_metrics else None
    net_cash = edinet_metrics.net_cash if edinet_metrics else None
    investment_securities = edinet_metrics.investment_securities if edinet_metrics else None
    edinet_failure_reasons = (
        ",".join((*edinet.failure_reasons, *(() if entity_matches else (ENTITY_SCALE_MISMATCH,))))
        if edinet
        else None
    )
    if net_cash is None and cash is not None and debt is not None:
        net_cash = cash - debt
    latest_market_cap = (latest_price * shares_ex_treasury) if shares_ex_treasury else None
    latest_enterprise_value = (
        (latest_market_cap + debt - cash)
        if latest_market_cap is not None and debt is not None and cash is not None
        else None
    )
    ev_ebitda = _safe_positive_ratio(latest_enterprise_value, ebitda_ttm)
    # 収益倍率も p_s / pcfr / ev_ebitda と同じ「時価総額 ÷ 円の TTM 系列」で組む。
    # 1 株当たりへの換算はここで 1 回だけ行い、市場が値付けできる株数で割る。こうすると
    # `株価 / eps == per_trailing` が厳密に成立し、同じ語が 2 つの値を指さない。
    per_trailing = _safe_positive_ratio(latest_market_cap, profit_ttm)
    eps_ttm = _safe_ratio(profit_ttm, shares_ex_treasury)
    # 純資産倍率も円で組む。自己資本は `総資産 x 開示自己資本比率` で出せるので 1 株当たり
    # 純資産を経由せずに済み、`bps` が四半期開示に載らないことによる古さを避けられる
    # (実測: 両方を持つ 4,032 銘柄で bps 経路の齢が中央値 90 日、円経路は 11 日、円経路が
    # 古い銘柄は 0)。
    #
    # **ただし 2 経路は同じ量とは限らない。** 自己資本比率の分子は優先株・非支配株主持分を
    # 含みうる一方、`bps` は普通株主に帰属する 1 株当たり純資産である。実測では通期行
    # 40,477 のうち 97.2% が 1% 以内で一致するが、447 行は円経路が 20% 以上大きく、155 行は
    # 2 倍を超える。円経路をそのまま使うと、その銘柄の PBR だけが割安側へ倒れる。
    #
    # 差は会社の資本構成から来るので銘柄ごとに判定できる。両方を持つ直近の行で突き合わせ、
    # 一致する会社だけ円経路の鮮度を使い、食い違う会社は普通株基準の `bps` を使う。
    # `total_assets` と `equity_to_asset_ratio` は同じ資本状態の組である。一方だけを新しい
    # 開示から採ると、資産変動率をそのまま自己資本へ混入させるため、両方を観測した最新行
    # から円経路を組む。個別の carry 値は表示・staleness fact として引き続き保持する。
    common_equity_row = _latest_complete_row(summaries, ("total_assets", "equity_to_asset_ratio"))
    bps_row = _latest_non_null_row(summaries, "bps")
    # 同一状態の円経路へ直しても、その組がより新しい BPS より古ければstaleな資本を
    # 復活させる。2経路のうち新しい観測を先に選び、同日または円経路が新しい場合だけ
    # 下の普通株basis一致判定へ進める。
    use_yen_route = common_equity_row is not None and (
        bps_row is None
        or _accounting_observation_key(common_equity_row) >= _accounting_observation_key(bps_row)
    )
    equity_yen = _common_equity_yen(
        summaries,
        total_assets=(
            common_equity_row.total_assets
            if common_equity_row is not None and use_yen_route
            else None
        ),
        equity_to_asset_ratio=(
            common_equity_row.equity_to_asset_ratio
            if common_equity_row is not None and use_yen_route
            else None
        ),
        bps=bps,
        shares_ex_treasury=shares_ex_treasury,
    )
    pbr = _safe_positive_ratio(latest_market_cap, equity_yen)
    prior_total_assets_row = _prior_year_summary(
        summaries,
        rules.ttm,
        field_name="total_assets",
    )
    accruals_to_assets = _accruals_to_assets(
        net_income=profit_ttm,
        ocf_ttm=ocf_ttm,
        total_assets=total_assets,
        prior_total_assets=(
            prior_total_assets_row.total_assets if prior_total_assets_row is not None else None
        ),
    )
    shares_prior = _prior_year_summary(summaries, rules.ttm, field_name="shares_outstanding")
    net_share_change_yoy = _yoy_ratio(
        shares_outstanding,
        shares_prior.shares_outstanding if shares_prior else None,
    )
    # 同じ前年行を、自己株式を除いた株数で測り直したもの。日本の自社株買いは取得した株式を
    # 自己株式へ入れるだけで発行済株式総数を減らさないので、上の量が動くのは主に消却年で
    # あって取得年ではない。どちらが forward 実現をよく説明するかは事前登録した比較で決める
    # ので (reports/studies/2026-08-17-tradable-share-change/)、ここでは両方を fact として
    # 出すだけで、carry の計算は変えない。
    tradable_share_change_yoy = _yoy_ratio(
        shares_ex_treasury,
        _shares_excluding_treasury(
            shares_prior.shares_outstanding if shares_prior else None,
            shares_prior.treasury_shares if shares_prior else None,
        ),
    )
    return FinancialSnapshot(
        latest_financial_disclosure_date=latest.disclosed_at if latest else None,
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
        sales=sales_row.sales if sales_row else None,
        cfo=cfo_row.cfo if cfo_row else None,
        cash_eq=cash_eq,
        total_assets=total_assets,
        market_price_yen=latest_price,
        shares_ex_treasury=shares_ex_treasury,
        capital_basis_failure_reason=capital_basis.failure_reason,
        market_cap=latest_market_cap,
        cash_to_market_cap=_safe_ratio(cash_eq, latest_market_cap),
        # 開示された自己資本比率をそのまま使う。`equity` は非支配株主持分を含む純資産なので
        # `equity / total_assets` は自己資本比率にならない。比率が観測できないときは None へ
        # 落とし、純資産比率で代用しない (代用は少数株主持分の大きい銘柄で比率を数 pt 過大に
        # し、`equity_ratio_min` の gate を通しやすくする向きに効く)。
        equity_ratio=equity_to_asset_ratio,
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
        capex_ttm=edinet_metrics.capex_ttm if edinet_metrics else None,
        depreciation_and_amortization_ttm=(
            edinet_metrics.depreciation_and_amortization_ttm if edinet_metrics else None
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
        eps_yoy=_yoy_ratio(eps_cumulative, eps_prior.eps_ttm if eps_prior else None),
        sales_yoy=_yoy_ratio(
            sales_row.sales if sales_row else None,
            sales_prior.sales if sales_prior else None,
        ),
        operating_profit_yoy=_yoy_ratio(operating_profit, operating_profit_prior_year),
        cfo_yoy=_yoy_ratio(
            cfo_row.cfo if cfo_row else None,
            cfo_prior.cfo if cfo_prior else None,
        ),
        operating_profit_loss_narrowing=_loss_narrowing(
            operating_profit,
            operating_profit_prior_year,
        ),
        ttm_quality_ev_ebitda=(
            edinet_metrics.ttm_quality_ev_ebitda if edinet_metrics else TTMQuality.UNAVAILABLE
        ),
        ttm_quality_per_trailing=profit_quality,
        ttm_quality_p_s=sales_quality,
        ttm_quality_pcfr=ocf_quality,
        ttm_quality_ocf_yield=ocf_quality,
        ttm_quality_sales=sales_quality,
        ttm_quality_fcf_yield=(
            edinet_metrics.ttm_quality_fcf if edinet_metrics else TTMQuality.UNAVAILABLE
        ),
        ttm_quality_net_cash=(
            edinet_metrics.ttm_quality_net_cash if edinet_metrics else TTMQuality.UNAVAILABLE
        ),
        shares_outstanding=shares_outstanding,
        accruals_to_assets=accruals_to_assets,
        net_share_change_yoy=net_share_change_yoy,
        tradable_share_change_yoy=tradable_share_change_yoy,
        bs_carry_forward_fields=bs_carry_forward_fields or None,
        bs_carry_forward_lag_days=bs_carry_forward_lag_days,
        forecast_special_gain_flag=forecast_special_gain_flag,
        forecast_full_year_loss_flag=forecast_full_year_loss_flag,
    )


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
    source = _latest_actual_row(summaries, field_names=(field_name,))
    if source is None:
        return None, None
    value = getattr(source, field_name)
    assert value is not None
    lag = max(0, (latest.disclosed_at - source.disclosed_at).days)
    return float(value), lag


def _latest_summary(summaries: Sequence[JQuantsFinancialSummary]) -> JQuantsFinancialSummary | None:
    return summaries[-1] if summaries else None


def _prior_year_summary(
    summaries: Sequence[JQuantsFinancialSummary],
    ttm_rules: TTMRules,
    *,
    field_name: str | None = None,
) -> JQuantsFinancialSummary | None:
    """Return the same fiscal period in the previous fiscal year.

    Assumes summaries are ordered oldest-first; revisions of the same fiscal
    period are resolved by taking the most recent occurrence.
    """
    latest = (
        _latest_actual_row(summaries, field_names=(field_name,))
        if field_name is not None
        else _latest_summary(summaries)
    )
    if latest is None:
        return None
    if latest.period_start is not None and latest.period_end is not None:
        matched = _matched_prior_period_summary(
            summaries,
            latest,
            ttm_rules,
            field_name=field_name,
        )
        if matched is not None:
            return matched
    if latest.fiscal_period is None or latest.fiscal_year_end is None:
        return None
    target_fiscal_year_end = _shift_year(latest.fiscal_year_end, -1)
    if target_fiscal_year_end is None:
        return None
    candidates = [
        summary
        for summary in _actual_rows(summaries)
        if field_name is None or getattr(summary, field_name) is not None
        if summary.fiscal_period == latest.fiscal_period
        and summary.fiscal_year_end == target_fiscal_year_end
    ]
    return max(candidates, key=lambda item: item.disclosed_at, default=None)


def _ttm_value(
    summaries: Sequence[JQuantsFinancialSummary],
    field: str,
    ttm_rules: TTMRules,
) -> tuple[float | None, TTMQuality]:
    """`直近累計 + 前期通期 - 前年同期間累計` で 12 か月へ直す。

    この合成は各項が同じ単位で加減できることを前提にする。円の総額は満たすが、1 株当たり
    の値は各項が自分の期の株数で割られているので満たさない。株数が動いた会社では黒字が
    赤字に見える。per-share の field を渡すのは呼び出し側の誤りなので受け付けない。
    """
    if field in _PER_SHARE_FIELDS:
        raise ValueError(f"{field} is per share; compose the yen line and convert once at the end")
    latest = _latest_summary(summaries)
    if (
        latest is None
        or getattr(latest, field) is None
        or latest.period_start is None
        or latest.period_end is None
    ):
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
    prior_same = _matched_prior_period_summary(
        summaries,
        latest,
        ttm_rules,
        field_name=field,
    )
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
    return max(candidates, key=lambda item: item.disclosed_at, default=None)


def _matched_prior_period_summary(
    summaries: Sequence[JQuantsFinancialSummary],
    latest: JQuantsFinancialSummary,
    ttm_rules: TTMRules,
    *,
    field_name: str | None = None,
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
    for summary in _actual_rows(summaries):
        if field_name is not None and getattr(summary, field_name) is None:
            continue
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
    return max(candidates, key=lambda item: item.disclosed_at, default=None)


def _period_days(summary: JQuantsFinancialSummary) -> int | None:
    if summary.period_start is None or summary.period_end is None:
        return None
    days = (summary.period_end - summary.period_start).days + 1
    return days if days > 0 else None


def _shift_months(value: date, months: int) -> date:
    """`months` か月前後の同じ日。月末をまたぐ日付は月内に丸める。

    配当の基準日を四半期末に置くために使う。期末が 3/31 の会社の中間基準日は 9/30 で、
    暦の日数ではなく月数で数えないと四半期の境界からずれる。
    """
    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    return date(year, month + 1, min(value.day, 28))


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
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar],
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
    prices = asof_basis_closes(eligible[-history_sessions:], adjustment_events, asof_date=asof_date)
    ev_ebitda_history: list[float] = []
    if (
        snapshot.shares_ex_treasury is not None
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
        and snapshot.shares_ex_treasury is not None
    ):
        p_s_history = [
            (price * snapshot.shares_ex_treasury) / snapshot.sales_ttm for price in prices
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

    Caller must ensure ``shares_ex_treasury`` / ``debt`` / ``cash`` are not
    None and ``ebitda_ttm`` is positive. v1 has only the latest balance sheet
    and TTM EBITDA, so those are held constant across price history while
    market cap varies with the (adjusted) historical close.

    Negative EV or non-positive EBITDA is outside the valuation multiple
    domain and is handled by cash / net-cash Valuation Approachs, not by EV/EBITDA mean
    reversion.
    """
    shares_ex_treasury = snapshot.shares_ex_treasury
    debt = snapshot.debt
    cash = snapshot.cash
    ebitda_ttm = snapshot.ebitda_ttm
    if shares_ex_treasury is None or debt is None or cash is None:
        raise ValueError("EV/EBITDA history requires shares, debt, and cash")
    if ebitda_ttm is None or ebitda_ttm <= 0:
        raise ValueError("EV/EBITDA history requires positive EBITDA")
    enterprise_value = (price * shares_ex_treasury) + debt - cash
    if enterprise_value <= 0:
        return None
    return enterprise_value / ebitda_ttm


def _price_change(
    bars: Sequence[JQuantsDailyBar],
    sessions: int,
    asof_date: date,
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar] | None = None,
) -> float | None:
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if len(ordered) <= sessions:
        return None
    # 分割を跨ぐ比較で偽の騰落を出さないよう、adjustment_factor から組んだ
    # asof 基準系列で両端を比較する (cache の adjustment_close は基準混在のため不使用)。
    prices = asof_basis_closes(ordered[-(sessions + 1) :], adjustment_events, asof_date=asof_date)
    base = prices[0]
    if base == 0:
        return None
    return (prices[-1] / base) - 1.0


def _realized_volatility(
    bars: Sequence[JQuantsDailyBar],
    sessions: int,
    asof_date: date,
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar] | None = None,
) -> float | None:
    """Annualized close-to-close volatility on one as-of-consistent share basis."""
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if len(ordered) <= sessions:
        return None
    prices = asof_basis_closes(ordered[-(sessions + 1) :], adjustment_events, asof_date=asof_date)
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
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar] | None = None,
) -> float | None:
    ordered = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at
    )
    if not ordered:
        return None
    prices = asof_basis_closes(ordered[-sessions:], adjustment_events, asof_date=asof_date)
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
    bars: Sequence[JQuantsDailyBar],
    asof_date: date,
    sessions: int,
    *,
    adjustment_events: Sequence[JQuantsAdjustmentFactorEvent | JQuantsDailyBar] | None = None,
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
    start = relevant[0].traded_at
    events = bars if adjustment_events is None else adjustment_events
    factors = [
        event.adjustment_factor
        for event in events
        if start <= event.traded_at <= asof_date and event.adjustment_factor is not None
    ]
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


def _edinet_describes_same_entity(
    edinet: EdinetMetricRecord | None,
    total_assets: float | None,
) -> bool:
    """EDINET の記録が短信と同じ実体を指しているか。

    抽出器は 1 つの書類を連結・単体のどちらかの基準で読む。連結財務諸表を持つ会社の
    書類を単体基準で読むと、負債・現金・EBITDA が親会社単独の値になる。時価総額と TTM
    系列は短信由来なので、比率の分子と分母が別の会社を指す (実測では総資産が 1/1000 に
    なる行がある)。両側が総資産を持つので、実体の一致は直接確かめられる。

    連結基準で読めた行は照合できた 3,117 行すべてで一致するため、総資産を持たない
    連結行はそのまま通す。単体基準・基準不明の行はその母集団の 17.6% が桁でずれており、
    どれがずれているかを他の field では言えないので、照合できなければ答えない。
    """
    if edinet is None:
        return True
    reported = edinet.total_assets
    if reported is not None and reported > 0 and total_assets is not None and total_assets > 0:
        ratio = reported / total_assets
        return 1 / ENTITY_SCALE_TOLERANCE <= ratio <= ENTITY_SCALE_TOLERANCE
    return edinet.consolidation_basis == CONSOLIDATED_BASIS


# 円経路の自己資本を普通株基準として採るために要求する一致幅。実測では通期行の 97.2% が
# 1% 以内に収まり、外れる 677 行は優先株・非支配株主持分を含む資本構成である。
_COMMON_EQUITY_BASIS_TOLERANCE = 0.05
_EQUITY_RATIO_REPORTING_HALF_UNIT = 0.0005
_BPS_REPORTING_HALF_UNIT = 0.005


def _reported_equity_routes_overlap(
    *,
    reported_ratio: float,
    bps: float,
    shares: float,
    total_assets: float,
) -> bool:
    """Whether the EqAR and BPS routes overlap inside their published precision."""

    ratio_low = reported_ratio - _EQUITY_RATIO_REPORTING_HALF_UNIT
    ratio_high = reported_ratio + _EQUITY_RATIO_REPORTING_HALF_UNIT
    bps_ratio_low = (bps - _BPS_REPORTING_HALF_UNIT) * shares / total_assets
    bps_ratio_high = (bps + _BPS_REPORTING_HALF_UNIT) * shares / total_assets
    return max(ratio_low, bps_ratio_low) <= min(ratio_high, bps_ratio_high)


def _common_equity_yen(
    summaries: Sequence[JQuantsFinancialSummary],
    *,
    total_assets: float | None,
    equity_to_asset_ratio: float | None,
    bps: float | None,
    shares_ex_treasury: float | None,
) -> float | None:
    """普通株主に帰属する自己資本 (円)。

    `総資産 x 自己資本比率` は四半期行にも載るので新しいが、分子が普通株主の持分とは
    限らない。`bps x 自己株控除後株数` は普通株基準だが古い。両方を持つ直近の行で 2 つが
    一致するなら、その会社では円経路も普通株基準なので鮮度を採る。食い違う会社は基準の
    違いなので `bps` 側を採る。どちらか一方しか無ければそれを使う。

    判定は突き合わせ行の中で行い、carry 後の 2 値が離れていることは理由にしない。乖離は
    ほとんどが実際の資本変動だからである — as-of 2026-07-31 で 1.5 倍を超えた 55 社のうち、
    未適用の分割で説明できるのは 1 社だけで、残り 54 社は `bps` の出所行から as-of まで
    調整係数を 1 つも持たない。倍率で切ると、減損や大幅増資で自己資本が実際に動いた会社の
    新しい値を捨てて古い `bps` を採ることになり、7069 (自己資本比率 0.441 -> 0.062) の
    PBR は 36.3 から 3.08 へ、割安側へ 12 倍ずれる。
    """
    yen_equity = (
        total_assets * equity_to_asset_ratio
        if total_assets is not None and equity_to_asset_ratio is not None
        else None
    )
    bps_equity = bps * shares_ex_treasury if bps is not None and shares_ex_treasury else None
    if yen_equity is None:
        return bps_equity
    if bps_equity is None:
        return yen_equity
    for summary in sorted(_actual_rows(summaries), key=_accounting_observation_key, reverse=True):
        if (
            summary.bps is None
            or summary.total_assets is None
            or summary.equity_to_asset_ratio is None
            or summary.shares_outstanding is None
        ):
            continue
        row_shares = _shares_excluding_treasury(summary.shares_outstanding, summary.treasury_shares)
        row_yen = summary.total_assets * summary.equity_to_asset_ratio
        if row_shares is None or row_yen <= 0:
            continue
        row_bps_equity = summary.bps * row_shares
        if row_bps_equity <= 0:
            continue
        agrees = abs(
            row_yen / row_bps_equity - 1.0
        ) <= _COMMON_EQUITY_BASIS_TOLERANCE or _reported_equity_routes_overlap(
            reported_ratio=summary.equity_to_asset_ratio,
            bps=summary.bps,
            shares=row_shares,
            total_assets=summary.total_assets,
        )
        return yen_equity if agrees else bps_equity
    # 突き合わせられる行が無い会社では、狭い方の基準を採る。
    return bps_equity


def _shares_excluding_treasury(
    shares_outstanding: float | None, treasury_shares: float | None
) -> float | None:
    """市場が値付けできる株式数。自己株式数が観測できない行は答えない。

    自己株式数の欠損を 0 で埋めると「自己株ゼロ」を捏造し、どれだけ過大か分からない
    時価総額が現金比率・利回り・流動性 gate へ入る。発行済を超える自己株式数も開示の破損
    なので答えない。どちらも時価総額が null になり、その銘柄は母集団に入らない。
    """

    if shares_outstanding is None or shares_outstanding <= 0:
        return None
    if treasury_shares is None or treasury_shares < 0:
        return None
    remaining = shares_outstanding - treasury_shares
    return remaining if remaining > 0 else None


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
    net_income: float | None,
    ocf_ttm: float | None,
    total_assets: float | None,
    prior_total_assets: float | None,
) -> float | None:
    """Sloan (1996) accruals ratio: (NI - CFO) / average total assets.

    NI is the reported profit line composed to TTM, not a product of a per-share
    figure and a share count: EPS is per average share excluding treasury while the
    issued count includes it, so the product describes neither. The denominator uses
    the average of current and prior-year total assets when both are present,
    otherwise the current value. Returns None for any missing input or zero
    denominator.
    """
    if net_income is None or ocf_ttm is None or total_assets is None:
        return None
    denominator = (
        (total_assets + prior_total_assets) / 2.0
        if prior_total_assets is not None and prior_total_assets > 0
        else total_assets
    )
    if denominator <= 0:
        return None
    return (net_income - ocf_ttm) / denominator


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
