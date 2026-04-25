from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import sqrt
from statistics import mean, median
from typing import Mapping, Sequence

from .providers.edinet import EdinetMetricRecord
from .providers.jquants import JQuantsDailyBar, JQuantsFinancialSummary
from .schema import DerivedMetrics, FinancialSnapshot, OperatingProfitSource, SecurityMaster, TTMQuality

VALUATION_METRICS = ("per_trailing", "pbr", "ev_ebitda")


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
) -> MetricBuildResult:
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
        )

    sector_metric_values: dict[str, dict[str, list[float]]] = {}
    for ticker, snapshot in financials.items():
        sector = securities_by_ticker[ticker].sector_33
        sector_bucket = sector_metric_values.setdefault(sector, {metric: [] for metric in VALUATION_METRICS})
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
        sector: (mean(values) - market_return_4w) if values and market_return_4w is not None else None
        for sector, values in sector_returns.items()
    }
    rs_percentiles = _rank_to_percentiles(sector_rs)

    derived: dict[str, DerivedMetrics] = {}
    for ticker, snapshot in financials.items():
        sector = securities_by_ticker[ticker].sector_33
        ticker_bars = bars_by_ticker.get(ticker, ())
        valuation_history = _valuation_history(latest_prices[ticker], ticker_bars, snapshot, asof_date)
        sector_gaps: dict[str, float | None] = {}
        self_percentiles: dict[str, float | None] = {}
        sigma_gaps: dict[str, float | None] = {}
        for metric in VALUATION_METRICS:
            current = getattr(snapshot, metric)
            sector_values = sector_metric_values.get(sector, {}).get(metric, [])
            baseline = sector_values if len(sector_values) >= 10 else market_metric_values.get(metric, [])
            sector_median = median(baseline) if baseline else None
            sector_gaps[metric] = ((current / sector_median) - 1.0) if current is not None and sector_median not in (None, 0) else None
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
            price_change_60d=_price_change(ticker_bars, 60, asof_date),
            sigma_gap=sigma_gaps,
            sector_relative_strength_4w=sector_rs.get(sector),
            sector_relative_strength_percentile=rs_percentiles.get(sector),
            ticker_return_4w=ticker_returns_4w.get(ticker),
            sector_return_4w=mean(sector_returns[sector]) if sector in sector_returns else None,
            short_history_flag=listing_span_days < 750,
        )

    ttm_quality_counts = _count_ttm_qualities(financials.values())
    yoy_missing_count = sum(
        1
        for snapshot in financials.values()
        if snapshot.eps_yoy is None or snapshot.sales_yoy is None or snapshot.operating_profit_yoy is None
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
) -> FinancialSnapshot:
    latest = _latest_summary(summaries)
    prior_year = _prior_year_summary(summaries)
    forecast_eps = latest.forecast_eps if latest else None
    eps_ttm = latest.eps_ttm if latest else None
    bps = latest.bps if latest else None
    per_forward = (latest_price / forecast_eps) if forecast_eps and forecast_eps > 0 else None
    per_trailing = (latest_price / eps_ttm) if eps_ttm and eps_ttm > 0 else None
    pbr = (latest_price / bps) if bps and bps > 0 else None
    operating_profit, operating_profit_source = _select_operating_profit(latest)
    operating_profit_prior_year, _ = _select_operating_profit(prior_year)
    sales_ttm = edinet.sales_ttm if edinet else None
    ocf_ttm = edinet.ocf_ttm if edinet else None
    debt = edinet.debt if edinet else None
    cash = edinet.cash if edinet else None
    ebitda_ttm = edinet.ebitda_ttm if edinet else None

    return FinancialSnapshot(
        per_forward=per_forward,
        per_trailing=per_trailing,
        pbr=pbr,
        ev_ebitda=_safe_ratio(((latest_price * latest.shares_outstanding) + debt - cash) if latest and latest.shares_outstanding and debt is not None and cash is not None else None, ebitda_ttm),
        p_s=_safe_ratio((latest_price * latest.shares_outstanding) if latest and latest.shares_outstanding else None, sales_ttm),
        pcfr=_safe_ratio((latest_price * latest.shares_outstanding) if latest and latest.shares_outstanding else None, ocf_ttm if ocf_ttm and ocf_ttm > 0 else None),
        eps=eps_ttm,
        sales_ttm=sales_ttm,
        ocf_ttm=ocf_ttm,
        debt=debt,
        cash=cash,
        ebitda_ttm=ebitda_ttm,
        consolidation_basis=edinet.consolidation_basis if edinet else None,
        operating_profit=operating_profit,
        operating_profit_source=operating_profit_source,
        eps_yoy=_yoy_ratio(eps_ttm, prior_year.eps_ttm if prior_year else None),
        sales_yoy=_yoy_ratio(latest.sales if latest else None, prior_year.sales if prior_year else None),
        operating_profit_yoy=_yoy_ratio(operating_profit, operating_profit_prior_year),
        ttm_quality_ev_ebitda=edinet.ttm_quality_ev_ebitda if edinet else TTMQuality.UNAVAILABLE,
        ttm_quality_p_s=edinet.ttm_quality_p_s if edinet else TTMQuality.UNAVAILABLE,
        ttm_quality_pcfr=edinet.ttm_quality_pcfr if edinet else TTMQuality.UNAVAILABLE,
    )


def _latest_summary(summaries: Sequence[JQuantsFinancialSummary]) -> JQuantsFinancialSummary | None:
    return summaries[-1] if summaries else None


def _prior_year_summary(summaries: Sequence[JQuantsFinancialSummary]) -> JQuantsFinancialSummary | None:
    """Return the current prior-year proxy from oldest-first summaries."""
    # J-Quants financial summaries are quarterly disclosures. The immediately
    # previous record is QoQ, so use four disclosures before latest as the
    # closest available prior-year quarter.
    # TODO(issue #24): replace this proxy with fiscal-period matching once
    # normalize_financial_summary stores TypeOfCurrentPeriod / CurrentFiscalYearEndDate.
    return summaries[-5] if len(summaries) >= 5 else None


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
    prices = [bar.close for bar in eligible[-750:]]
    # FIXME(issue #15): ev_ebitda の history 近似は展開すると price / snapshot.ev_ebitda に縮退する。
    history: dict[str, list[float]] = {
        "per_trailing": [(price / snapshot.eps) for price in prices if snapshot.eps and snapshot.eps > 0],
        "pbr": [(price / (latest_price / snapshot.pbr)) for price in prices if snapshot.pbr not in (None, 0)],
        "ev_ebitda": [
            ((price / latest_price) * ((latest_price / snapshot.ev_ebitda) * snapshot.ebitda_ttm) / snapshot.ebitda_ttm)
            for price in prices
            if snapshot.ev_ebitda is not None and snapshot.ebitda_ttm not in (None, 0) and latest_price != 0
        ],
    }
    return history


def _price_change(bars: Sequence[JQuantsDailyBar], sessions: int, asof_date: date) -> float | None:
    ordered = sorted((bar for bar in bars if bar.traded_at <= asof_date), key=lambda item: item.traded_at)
    if len(ordered) <= sessions:
        return None
    current = ordered[-1].close
    base = ordered[-(sessions + 1)].close
    if base == 0:
        return None
    return (current / base) - 1.0


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
    if numerator is None or denominator in (None, 0):
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
    if current is None or previous in (None, 0):
        return None
    return (current / previous) - 1.0


def _count_ttm_qualities(snapshots: Sequence[FinancialSnapshot]) -> dict[str, int]:
    counts = {quality.value: 0 for quality in TTMQuality}
    for snapshot in snapshots:
        for quality in (
            snapshot.ttm_quality_ev_ebitda,
            snapshot.ttm_quality_p_s,
            snapshot.ttm_quality_pcfr,
        ):
            counts[quality.value] += 1
    return counts


def _rank_to_percentiles(values: Mapping[str, float | None]) -> dict[str, float | None]:
    available = sorted((value, key) for key, value in values.items() if value is not None)
    if not available:
        return {key: None for key in values}
    total = len(available)
    output: dict[str, float | None] = {key: None for key in values}
    for index, (_, key) in enumerate(available, start=1):
        output[key] = index / total
    return output
