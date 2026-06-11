from __future__ import annotations

from datetime import date

import pytest

from baibai_loop.ledger.exposure import compute_exposure
from baibai_loop.ledger.trades import TradeRecord


def _trade(
    ticker: str,
    entry_price: float,
    quantity: int,
    playbook_id: str | None = None,
    current_quantity: int | None = None,
) -> TradeRecord:
    return TradeRecord(
        trade_id=f"trade-{ticker}",
        ticker=ticker,
        name="Sample",
        position_state="open",
        review_state="scheduled",
        entry_date=date(2026, 5, 7),
        quantity=quantity,
        entry_price=entry_price,
        playbook_id=playbook_id,
        current_quantity=current_quantity,
    )


def test_compute_exposure_aggregates_sector_shares_by_entry_notional() -> None:
    trades = [
        _trade("9682", 1000.0, 200, "sales-discount-growth"),
        _trade("9692", 2000.0, 100, "sales-discount-growth"),
        _trade("8255", 1000.0, 100, "cashflow-yield-discount"),
    ]
    sectors = {"9682": "情報・通信業", "9692": "情報・通信業", "8255": "小売業"}
    report = compute_exposure(trades, sectors)
    assert report.total_notional == pytest.approx(500_000.0)
    top_sector = report.by_sector[0]
    assert top_sector.key == "情報・通信業"
    assert top_sector.share == pytest.approx(0.8)
    assert top_sector.tickers == ("9682", "9692")


def test_compute_exposure_warns_when_sector_and_playbook_exceed_half() -> None:
    trades = [
        _trade("9682", 1000.0, 200, "sales-discount-growth"),
        _trade("9692", 2000.0, 100, "sales-discount-growth"),
        _trade("8255", 1000.0, 100, "cashflow-yield-discount"),
    ]
    sectors = {"9682": "情報・通信業", "9692": "情報・通信業", "8255": "小売業"}
    report = compute_exposure(trades, sectors)
    assert any("sector 情報・通信業" in warning for warning in report.warnings)
    assert any("playbook sales-discount-growth" in warning for warning in report.warnings)


def test_compute_exposure_does_not_warn_at_exact_half() -> None:
    trades = [
        _trade("0001", 1000.0, 100, "a"),
        _trade("0002", 1000.0, 100, "b"),
    ]
    sectors = {"0001": "機械", "0002": "小売業"}
    report = compute_exposure(trades, sectors)
    assert report.warnings == ()


def test_compute_exposure_buckets_missing_facts_as_unknown() -> None:
    trades = [_trade("0001", 1000.0, 100, None)]
    report = compute_exposure(trades, {})
    assert report.by_sector[0].key == "unknown"
    assert report.by_playbook[0].key == "unknown"
    assert report.by_sector[0].share == pytest.approx(1.0)


def test_compute_exposure_sizes_partial_exits_by_current_quantity() -> None:
    trades = [
        _trade("0001", 1000.0, 200, "a", current_quantity=100),
        _trade("0002", 1000.0, 100, "b"),
    ]
    sectors = {"0001": "機械", "0002": "小売業"}
    report = compute_exposure(trades, sectors)
    assert report.total_notional == pytest.approx(200_000.0)
    assert report.by_ticker[0].share == pytest.approx(0.5)
    assert report.warnings == ()


def test_compute_exposure_handles_zero_notional() -> None:
    report = compute_exposure([], {})
    assert report.total_notional == 0.0
    assert report.by_sector == ()
    assert report.warnings == ()
