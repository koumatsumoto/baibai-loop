from __future__ import annotations

from datetime import date

import pytest

from baibai_loop.ledger.benchmark import compute_forward_performance
from baibai_loop.ledger.trades import TradeRecord
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


def _trade(ticker: str, entry_date: date, entry_price: float, quantity: int) -> TradeRecord:
    return TradeRecord(
        trade_id=f"trade-{ticker}",
        ticker=ticker,
        name="Sample",
        position_state="open",
        review_state="scheduled",
        entry_date=entry_date,
        quantity=quantity,
        entry_price=entry_price,
    )


def test_compute_forward_performance_returns_relative_to_benchmark() -> None:
    trades = [_trade("9682", date(2026, 5, 7), 1000.0, 100)]
    bars = [
        _bar("9682", date(2026, 5, 7), 1000.0),
        _bar("9682", date(2026, 6, 5), 1100.0),
        _bar("1321", date(2026, 5, 7), 1000.0),
        _bar("1321", date(2026, 6, 5), 1050.0),
    ]
    result = compute_forward_performance(trades, date(2026, 6, 5), bars)
    position = result.positions[0]
    assert position.return_ratio == pytest.approx(0.10)
    assert position.benchmark_return == pytest.approx(0.05)
    assert position.relative == pytest.approx(0.05)
    assert result.relative == pytest.approx(0.05)
    assert result.warnings == ()


def test_compute_forward_performance_weights_benchmark_by_entry_notional() -> None:
    trades = [
        _trade("0001", date(2026, 5, 7), 1000.0, 100),  # notional 100k
        _trade("0002", date(2026, 5, 14), 1000.0, 300),  # notional 300k
    ]
    bars = [
        _bar("0001", date(2026, 5, 7), 1000.0),
        _bar("0001", date(2026, 6, 5), 1000.0),
        _bar("0002", date(2026, 5, 14), 1000.0),
        _bar("0002", date(2026, 6, 5), 1000.0),
        _bar("1321", date(2026, 5, 7), 1000.0),
        _bar("1321", date(2026, 5, 14), 1000.0),
        _bar("1321", date(2026, 6, 5), 1100.0),  # +10% from both entries
    ]
    result = compute_forward_performance(trades, date(2026, 6, 5), bars)
    assert result.benchmark_return == pytest.approx(0.10)


def test_compute_forward_performance_resolves_price_before_asof() -> None:
    trades = [_trade("9682", date(2026, 5, 7), 1000.0, 100)]
    bars = [
        _bar("9682", date(2026, 5, 7), 1000.0),
        _bar("9682", date(2026, 6, 3), 1200.0),  # latest before a 2026-06-06 asof
        _bar("1321", date(2026, 5, 7), 1000.0),
        _bar("1321", date(2026, 6, 3), 1000.0),
    ]
    result = compute_forward_performance(trades, date(2026, 6, 6), bars)
    assert result.positions[0].eval_price == 1200.0


def test_compute_forward_performance_warns_on_missing_benchmark() -> None:
    trades = [_trade("9682", date(2026, 5, 7), 1000.0, 100)]
    bars = [
        _bar("9682", date(2026, 5, 7), 1000.0),
        _bar("9682", date(2026, 6, 5), 1100.0),
    ]
    result = compute_forward_performance(trades, date(2026, 6, 5), bars)
    assert result.benchmark_return is None
    assert result.relative is None
    assert any("benchmark" in warning for warning in result.warnings)
