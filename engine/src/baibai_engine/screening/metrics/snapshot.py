"""source factからFinancialSnapshotとScreeningMetricsを組み立てる。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from datetime import date, timedelta
from statistics import mean, median

from baibai_engine.market.bars import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
    asof_basis_closes,
)
from baibai_engine.market.jquants_models import JQuantsFinancialSummary
from baibai_engine.market.models import SecurityMaster
from baibai_engine.market.providers.edinet import EdinetMetricRecord
from baibai_engine.screening.margin_metrics import MarginBalance, margin_supply_demand
from baibai_engine.screening.metrics.capital import (
    ENTITY_SCALE_MISMATCH,
    _common_equity_yen,
    _edinet_describes_same_entity,
    _normalize_summaries_with_status,
    _resolve_capital_basis,
    _shares_excluding_treasury,
)
from baibai_engine.screening.metrics.dividends import _resolve_dividend_carry
from baibai_engine.screening.metrics.history import (
    AVG_VOLUME_SESSIONS,
    PRICE_HISTORY_WINDOW_DAYS,
    VALUATION_HISTORY_SESSIONS,
    _avg_daily_volume,
    _gap_from_low,
    _has_split_adjustment_within_sessions,
    _latest_bar_on_or_before,
    _price_change,
    _realized_volatility,
    _self_range_percentile,
    _sigma_gap,
    _turnover_spike,
    _valuation_history,
)
from baibai_engine.screening.metrics.periods import (
    _accounting_observation_key,
    _AccountingObservationKey,
    _carry_forward,
    _latest_actual_row,
    _latest_actual_row_by_field_priority,
    _latest_complete_row,
    _latest_non_null_row,
    _latest_summary,
    _prior_year_summary,
    _ttm_value,
)
from baibai_engine.screening.metrics.profit import (
    _accruals_to_assets,
    _loss_narrowing,
    _resolve_earnings_forecast,
    _select_operating_profit,
)
from baibai_engine.screening.metrics.ratios import _safe_positive_ratio, _safe_ratio, _yoy_ratio
from baibai_engine.screening.rule_config import ScreeningRules, load_screening_rules
from baibai_engine.screening.schema import (
    SECTOR_MEDIAN_BASIS_MARKET,
    SECTOR_MEDIAN_BASIS_SECTOR,
    DerivedMetrics,
    FinancialSnapshot,
    OperatingProfitSource,
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
# TTMは非実績行を除外し、選択した各実績期間の欠損を古いrevisionで埋めない。
VALUATION_CALCULATION_REVISION = "same-profit-basis-yoy-v25"

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


@dataclass(frozen=True)
class MetricBuildResult:
    financials: Mapping[str, FinancialSnapshot]
    derived: Mapping[str, DerivedMetrics]
    ttm_quality_counts: Mapping[str, int]
    yoy_missing_count: int


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
    # 同じ予想期の純利益>経常利益はdata-quality注記。原因・持続性は一次開示で確認する。
    # 純利益/経常は forecast_eps と同一予想期のペアで ingest 済み・分割不変の絶対額なので、
    # 両方揃うときだけ比較する。flag は warning で per_forward / E[r] / rank を変えない。
    forecast_profit = forecast.profit if forecast is not None else None
    forecast_ordinary_profit = forecast.ordinary_profit if forecast is not None else None
    forecast_special_gain_flag = (
        forecast_profit is not None
        and forecast_ordinary_profit is not None
        and forecast_profit > forecast_ordinary_profit
    )
    # 予想経常利益または予想純利益の赤字を注記する。
    # flagだけでは予想EPSの符号や採用FV anchorは確定しない。
    # 未開示と赤字の確認を区別する。
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
    operating_profit_prior_year = (
        getattr(operating_prior_row, operating_field)
        if operating_prior_row is not None and operating_field is not None
        else None
    )
    capital_basis = _resolve_capital_basis(
        summaries,
        capital_basis_barrier=capital_basis_barrier,
    )
    shares_outstanding = capital_basis.issued
    # 時価総額の分母は自己株式を除いた株数である。自己株式は議決権も配当請求権も持たない
    # ので、含めると時価総額が過大になり現金比率・利回りが薄く、倍率が割高に見える。歪みが
    # 最大になるのは自己株式を積み上げた企業である。自己株式数が観測できない行は
    # 時価総額を出さない — 発行済で
    # 代用すると、どれだけ過大かが分からない値が現金比率・利回り・時価総額 gate へ入る。
    shares_ex_treasury = capital_basis.shares_ex_treasury
    operating_profit_ttm, _ = _ttm_value(summaries, "operating_profit", rules.ttm)
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
    same_state_equity_yen = None
    if common_equity_row is not None:
        assert common_equity_row.total_assets is not None
        assert common_equity_row.equity_to_asset_ratio is not None
        same_state_equity_yen = (
            common_equity_row.total_assets * common_equity_row.equity_to_asset_ratio
        )
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
        same_state_equity_yen=same_state_equity_yen,
        market_price_yen=latest_price,
        shares_ex_treasury=shares_ex_treasury,
        capital_basis_failure_reason=capital_basis.failure_reason,
        market_cap=latest_market_cap,
        cash_to_market_cap=_safe_ratio(cash_eq, latest_market_cap),
        # 開示された自己資本比率をそのまま使う。`equity` は非支配株主持分を含む純資産なので
        # `equity / total_assets` は自己資本比率にならない。比率が観測できないときは None へ
        # 落とし、純資産比率で代用しない。非支配株主持分が大きい銘柄ほど過大評価になる。
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
        operating_profit_ttm=operating_profit_ttm,
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
        ttm_quality_ev_ebitda=(edinet_metrics.ttm_quality_ev_ebitda if edinet_metrics else None),
        ttm_quality_per_trailing=profit_quality,
        ttm_quality_p_s=sales_quality,
        ttm_quality_pcfr=ocf_quality,
        ttm_quality_ocf_yield=ocf_quality,
        ttm_quality_sales=sales_quality,
        ttm_quality_fcf_yield=(edinet_metrics.ttm_quality_fcf if edinet_metrics else None),
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


_TTM_QUALITY_FIELDS = tuple(
    field.name for field in fields(FinancialSnapshot) if field.name.startswith("ttm_quality_")
)


def _count_ttm_qualities(snapshots: Sequence[FinancialSnapshot]) -> dict[str, int]:
    # 集計対象は schema の ttm_quality_* field から導出する (次元を足したとき
    # ここの列挙更新漏れで集計 telemetry から欠落するのを防ぐ)。
    counts = {quality.value: 0 for quality in TTMQuality}
    for snapshot in snapshots:
        for field_name in _TTM_QUALITY_FIELDS:
            quality: TTMQuality | None = getattr(snapshot, field_name)
            if quality is not None:
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
