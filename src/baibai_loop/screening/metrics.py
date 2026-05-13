from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from math import sqrt
from statistics import mean, median

from .providers.edinet import EdinetMetricRecord
from .providers.jquants import JQuantsDailyBar, JQuantsFinancialSummary
from .rule_config import ScreeningRules, TTMRules, load_screening_rules
from .schema import (
    DerivedMetrics,
    FinancialSnapshot,
    OperatingProfitSource,
    SecurityMaster,
    TTMQuality,
)

VALUATION_METRICS = ("per_trailing", "pbr", "ev_ebitda", "p_s")


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
) -> MetricBuildResult:
    rules = rules or load_screening_rules()
    financials: dict[str, FinancialSnapshot] = {}
    latest_prices: dict[str, float] = {}

    for ticker, security in securities_by_ticker.items():
        del security
        latest_bar = _latest_bar_on_or_before(bars_by_ticker.get(ticker, ()), asof_date)
        if latest_bar is None:
            continue
        latest_prices[ticker] = latest_bar.close
        financials[ticker] = _build_financial_snapshot(
            latest_price=latest_bar.close,
            summaries=summaries_by_ticker.get(ticker, ()),
            edinet=edinet_by_ticker.get(ticker),
            rules=rules,
        )

    sector_metric_values: dict[str, dict[str, list[float]]] = {}
    for ticker, snapshot in financials.items():
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
            for snapshot in financials.values()
            if getattr(snapshot, metric) is not None
        ]
        for metric in VALUATION_METRICS
    }

    sector_returns: dict[str, list[float]] = {}
    ticker_returns_4w: dict[str, float] = {}
    for ticker, security in securities_by_ticker.items():
        four_week = _price_change(bars_by_ticker.get(ticker, ()), 20, asof_date)
        if four_week is not None:
            ticker_returns_4w[ticker] = four_week
            sector_returns.setdefault(security.sector_33, []).append(four_week)

    market_return_4w = mean(ticker_returns_4w.values()) if ticker_returns_4w else None
    sector_rs = {
        sector: (mean(values) - market_return_4w)
        if values and market_return_4w is not None
        else None
        for sector, values in sector_returns.items()
    }
    rs_percentiles = _rank_to_percentiles(sector_rs)

    derived: dict[str, DerivedMetrics] = {}
    for ticker, snapshot in financials.items():
        sector = securities_by_ticker[ticker].sector_33
        ticker_bars = bars_by_ticker.get(ticker, ())
        valuation_history = _valuation_history(
            latest_prices[ticker], ticker_bars, snapshot, asof_date
        )
        sector_gaps: dict[str, float | None] = {}
        self_percentiles: dict[str, float | None] = {}
        sigma_gaps: dict[str, float | None] = {}
        for metric in VALUATION_METRICS:
            current = getattr(snapshot, metric)
            sector_values = sector_metric_values.get(sector, {}).get(metric, [])
            baseline = (
                sector_values if len(sector_values) >= 10 else market_metric_values.get(metric, [])
            )
            sector_median = median(baseline) if baseline else None
            sector_gaps[metric] = (
                ((current / sector_median) - 1.0)
                if current is not None and sector_median not in (None, 0)
                else None
            )
            history_values = valuation_history.get(metric, [])
            self_percentiles[metric] = _self_range_percentile(history_values, current)
            sigma_gaps[metric] = _sigma_gap(history_values, current)

        eligible_bars = sorted(
            (bar for bar in ticker_bars if bar.traded_at <= asof_date),
            key=lambda item: item.traded_at,
        )
        listing_span_days = (asof_date - eligible_bars[0].traded_at).days if eligible_bars else 0
        derived[ticker] = DerivedMetrics(
            sector_median_gap=sector_gaps,
            self_range_percentile=self_percentiles,
            price_change_1d=_price_change(ticker_bars, 1, asof_date),
            price_change_5d=_price_change(ticker_bars, 5, asof_date),
            price_change_20d=_price_change(ticker_bars, 20, asof_date),
            price_change_60d=_price_change(ticker_bars, 60, asof_date),
            gap_from_52w_low=_gap_from_low(ticker_bars, 252, asof_date),
            turnover_spike_5d=_turnover_spike(ticker_bars, asof_date),
            sigma_gap=sigma_gaps,
            sector_relative_strength_4w=sector_rs.get(sector),
            sector_relative_strength_percentile=rs_percentiles.get(sector),
            ticker_return_4w=ticker_returns_4w.get(ticker),
            sector_return_4w=mean(sector_returns[sector]) if sector in sector_returns else None,
            short_history_flag=listing_span_days < 750,
            split_adjustment_flag=_has_split_adjustment_within_sessions(ticker_bars, asof_date, 60),
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


def build_shares_outstanding_index(
    summaries_by_ticker: Mapping[str, Sequence[JQuantsFinancialSummary]],
) -> dict[str, float | None]:
    shares: dict[str, float | None] = {}
    for ticker, summaries in summaries_by_ticker.items():
        latest = _latest_summary(summaries)
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


def _build_financial_snapshot(
    latest_price: float,
    summaries: Sequence[JQuantsFinancialSummary],
    edinet: EdinetMetricRecord | None,
    rules: ScreeningRules,
) -> FinancialSnapshot:
    latest = _latest_summary(summaries)
    prior_year = _prior_year_summary(summaries, rules.ttm)
    forecast_eps = latest.forecast_eps if latest else None
    eps_ttm = latest.eps_ttm if latest else None
    bps = latest.bps if latest else None
    per_forward = (latest_price / forecast_eps) if forecast_eps and forecast_eps > 0 else None
    per_trailing = (latest_price / eps_ttm) if eps_ttm and eps_ttm > 0 else None
    pbr = (latest_price / bps) if bps and bps > 0 else None
    operating_profit, operating_profit_source = _select_operating_profit(latest)
    operating_profit_prior_year, _ = _select_operating_profit(prior_year)
    shares_outstanding = latest.shares_outstanding if latest else None
    sales_ttm, sales_quality = _ttm_value(summaries, "sales", rules.ttm)
    ocf_ttm, ocf_quality = _ttm_value(summaries, "cfo", rules.ttm)
    edinet_ocf_ttm = edinet.ocf_ttm if edinet else None
    debt = edinet.debt if edinet else None
    cash = edinet.cash if edinet else None
    ebitda_ttm = edinet.ebitda_ttm if edinet else None
    fcf_ttm = edinet.fcf_ttm if edinet else None
    net_cash = edinet.net_cash if edinet else None
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

    return FinancialSnapshot(
        per_forward=per_forward,
        per_trailing=per_trailing,
        pbr=pbr,
        ev_ebitda=ev_ebitda,
        p_s=_safe_ratio(latest_market_cap, sales_ttm),
        pcfr=_safe_ratio(latest_market_cap, ocf_ttm if ocf_ttm and ocf_ttm > 0 else None),
        eps=eps_ttm,
        sales_ttm=sales_ttm,
        ocf_ttm=ocf_ttm,
        edinet_ocf_ttm=edinet_ocf_ttm,
        sales=latest.sales if latest else None,
        cfo=latest.cfo if latest else None,
        cash_eq=latest.cash_eq if latest else None,
        total_assets=latest.total_assets if latest else None,
        equity=latest.equity if latest else None,
        market_cap=latest_market_cap,
        cash_to_market_cap=_safe_ratio(latest.cash_eq if latest else None, latest_market_cap),
        price_to_equity=_safe_ratio(latest_market_cap, latest.equity if latest else None),
        equity_ratio=_safe_ratio(
            latest.equity if latest else None, latest.total_assets if latest else None
        ),
        ocf_yield=_safe_ratio(ocf_ttm, latest_market_cap),
        net_cash=net_cash,
        net_cash_to_market_cap=_safe_ratio(net_cash, latest_market_cap),
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
        eps_yoy=_yoy_ratio(eps_ttm, prior_year.eps_ttm if prior_year else None),
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
        ttm_quality_p_s=sales_quality,
        ttm_quality_pcfr=ocf_quality,
        ttm_quality_ocf_yield=ocf_quality,
        ttm_quality_sales=sales_quality,
        ttm_quality_fcf_yield=edinet.ttm_quality_fcf if edinet else TTMQuality.UNAVAILABLE,
        ttm_quality_net_cash=edinet.ttm_quality_net_cash if edinet else TTMQuality.UNAVAILABLE,
        shares_outstanding=shares_outstanding,
    )


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
) -> dict[str, list[float]]:
    # asof 以前の bar に限定し、look-ahead bias を防ぐ。
    eligible = sorted(
        (bar for bar in bars if bar.traded_at <= asof_date),
        key=lambda item: item.traded_at,
    )
    # Prefer split-adjusted close so price discontinuities at split dates do
    # not propagate into historical valuation series. PBR/PER history happens
    # to cancel raw price discontinuities through their proportional form, but
    # EV/EBITDA carries a constant net-debt offset that does not, so adjusted
    # close is required for at least that series; using it everywhere keeps
    # the three histories on the same price basis.
    prices = [
        bar.adjustment_close if bar.adjustment_close is not None else bar.close
        for bar in eligible[-750:]
    ]
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

    history: dict[str, list[float]] = {
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
    domain and is handled by cash / net-cash lanes, not by EV/EBITDA mean
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
    # Prefer split-adjusted close on both ends so a stock split between the two
    # dates does not show up as a synthetic price drop. Fall back to raw close
    # only when the adjustment field is absent (legacy bars).
    current = _adjusted_close(ordered[-1])
    base_bar = ordered[-(sessions + 1)]
    base = _adjusted_close(base_bar)
    if base == 0:
        return None
    return (current / base) - 1.0


def _adjusted_close(bar: JQuantsDailyBar) -> float:
    return bar.adjustment_close if bar.adjustment_close is not None else bar.close


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
    prices = [_adjusted_close(bar) for bar in ordered[-sessions:]]
    current = prices[-1]
    low = min(prices)
    if low <= 0:
        return None
    return (current / low) - 1.0


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


def _loss_narrowing(current: float | None, previous: float | None) -> bool | None:
    if current is None or previous is None:
        return None
    if current >= 0:
        return False
    return previous < 0 and current > previous


def _count_ttm_qualities(snapshots: Sequence[FinancialSnapshot]) -> dict[str, int]:
    counts = {quality.value: 0 for quality in TTMQuality}
    for snapshot in snapshots:
        for quality in (
            snapshot.ttm_quality_ev_ebitda,
            snapshot.ttm_quality_p_s,
            snapshot.ttm_quality_pcfr,
            snapshot.ttm_quality_ocf_yield,
            snapshot.ttm_quality_sales,
            snapshot.ttm_quality_fcf_yield,
            snapshot.ttm_quality_net_cash,
        ):
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
