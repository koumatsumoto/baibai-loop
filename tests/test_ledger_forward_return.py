from __future__ import annotations

from datetime import date

import pytest

from baibai_loop.ledger.forward_return import (
    aggregate_forward_returns,
    compute_ticker_forward_returns,
    latest_bar_date,
)
from baibai_loop.screening.providers.jquants import JQuantsDailyBar


def _bar(ticker: str, traded_at: date, close: float) -> JQuantsDailyBar:
    return JQuantsDailyBar(
        ticker=ticker,
        traded_at=traded_at,
        close=close,
        turnover_value=None,
        adjustment_close=close,
        adjustment_factor=1.0,
    )


def test_compute_ticker_forward_returns_resolves_each_horizon() -> None:
    asof = date(2026, 4, 10)
    bars = [
        _bar("9682", asof, 1000.0),
        _bar("9682", date(2026, 4, 17), 1050.0),  # +1w
        _bar("9682", date(2026, 5, 8), 1100.0),  # +4w
        _bar("1321", asof, 1000.0),
        _bar("1321", date(2026, 4, 17), 1010.0),
        _bar("1321", date(2026, 5, 8), 1020.0),
    ]
    result = compute_ticker_forward_returns("9682", asof, bars, eval_cap=date(2026, 5, 8))
    by_weeks = {horizon.weeks: horizon for horizon in result.horizons}
    assert by_weeks[1].return_ratio == pytest.approx(0.05)
    assert by_weeks[1].relative == pytest.approx(0.04)
    assert by_weeks[4].return_ratio == pytest.approx(0.10)


def test_compute_ticker_forward_returns_marks_future_horizon_unresolved() -> None:
    asof = date(2026, 5, 29)
    bars = [
        _bar("9682", asof, 1000.0),
        _bar("9682", date(2026, 6, 5), 1100.0),  # +1w only
        _bar("1321", asof, 1000.0),
        _bar("1321", date(2026, 6, 5), 1050.0),
    ]
    # eval_cap is the last cached bar; +4w target is beyond it.
    result = compute_ticker_forward_returns("9682", asof, bars, eval_cap=date(2026, 6, 5))
    by_weeks = {horizon.weeks: horizon for horizon in result.horizons}
    assert by_weeks[1].resolved is True
    assert by_weeks[4].resolved is False
    assert by_weeks[4].return_ratio is None


def test_compute_ticker_forward_returns_resolves_target_on_or_before() -> None:
    asof = date(2026, 4, 10)
    bars = [
        _bar("9682", asof, 1000.0),
        # +1w target is 2026-04-17 (Fri); only a 2026-04-16 bar exists -> use it.
        _bar("9682", date(2026, 4, 16), 1080.0),
        _bar("1321", asof, 1000.0),
        _bar("1321", date(2026, 4, 16), 1000.0),
    ]
    result = compute_ticker_forward_returns(
        "9682", asof, bars, eval_cap=date(2026, 6, 5), horizon_weeks=(1,)
    )
    assert result.horizons[0].price == 1080.0


def test_aggregate_forward_returns_skips_unresolved() -> None:
    asof = date(2026, 5, 29)
    bars = [
        _bar("9682", asof, 1000.0),
        _bar("9682", date(2026, 6, 5), 1100.0),
        _bar("9692", asof, 2000.0),
        _bar("9692", date(2026, 6, 5), 2100.0),
        _bar("1321", asof, 1000.0),
        _bar("1321", date(2026, 6, 5), 1050.0),
    ]
    results = [
        compute_ticker_forward_returns("9682", asof, bars, eval_cap=date(2026, 6, 5)),
        compute_ticker_forward_returns("9692", asof, bars, eval_cap=date(2026, 6, 5)),
    ]
    aggregates = {agg.weeks: agg for agg in aggregate_forward_returns(results)}
    assert aggregates[1].count == 2
    assert aggregates[1].mean_return == pytest.approx((0.10 + 0.05) / 2)
    # +4w / +8w are beyond eval_cap for a 2026-05-29 asof.
    assert aggregates[4].count == 0
    assert aggregates[4].mean_return is None


def test_latest_bar_date_returns_max() -> None:
    bars = [
        _bar("9682", date(2026, 5, 1), 1.0),
        _bar("9682", date(2026, 6, 5), 1.0),
    ]
    assert latest_bar_date(bars) == date(2026, 6, 5)
