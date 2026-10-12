"""価格観測の読取と、保存したsplit eventに基づく価格のみの比較算術。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from baibai_engine.foundation.sqlite_read import read_rows
from baibai_engine.market.bars import asof_basis_prices


@dataclass(frozen=True, slots=True)
class PriceBar:
    traded_at: date
    price: float
    turnover_value: float | None


def read_price_bars(
    sqlite_path: Path,
    *,
    tickers: Sequence[str] | None = None,
    start: date,
    end: date,
) -> dict[str, list[PriceBar]]:
    if tickers == ():
        return {}
    clause = ""
    parameters: tuple[object, ...] = (start.isoformat(), end.isoformat())
    if tickers is not None:
        clause = " AND ticker IN (" + ",".join("?" for _ in tickers) + ")"
        parameters += tuple(tickers)
    rows = read_rows(
        sqlite_path,
        "SELECT ticker, traded_at, close, adjustment_factor, turnover_value "  # nosec B608
        "FROM jquants_daily_bars WHERE traded_at >= ? AND traded_at <= ?"
        + clause
        + " ORDER BY ticker, traded_at",
        parameters,
    )
    # cache の adjustment_close は incremental 取得で遡及の有無が混在するため使わず、
    # 不変イベントの adjustment_factor の後方累積で末尾基準の価格系列を組む。
    raw: dict[str, list[tuple[date, float | None, float | None, float | None]]] = {}
    for ticker, traded_at, close, adjustment_factor, turnover_value in rows:
        if not isinstance(traded_at, str):
            continue
        raw.setdefault(str(ticker), []).append(
            (
                date.fromisoformat(traded_at),
                float(close) if isinstance(close, int | float) else None,
                float(adjustment_factor) if isinstance(adjustment_factor, int | float) else None,
                float(turnover_value) if isinstance(turnover_value, int | float) else None,
            )
        )
    bars: dict[str, list[PriceBar]] = {}
    for ticker, entries in raw.items():
        valued = [
            (traded_at, close, turnover)
            for traded_at, close, _, turnover in entries
            if close is not None
        ]
        adjusted = asof_basis_prices(
            [(traded_at, close) for traded_at, close, _ in valued],
            [(traded_at, adjustment_factor) for traded_at, _, adjustment_factor, _ in entries],
            asof_date=end,
        )
        bars[ticker] = [
            PriceBar(traded_at=traded_at, price=price, turnover_value=turnover)
            for (traded_at, _, turnover), price in zip(valued, adjusted, strict=True)
        ]
    return bars
