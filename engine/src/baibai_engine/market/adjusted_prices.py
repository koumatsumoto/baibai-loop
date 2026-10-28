"""価格観測の読取と、保存した価格調整係数に基づく価格のみの比較算術。"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
from datetime import date
from itertools import groupby
from pathlib import Path

from baibai_engine.storage.sqlite.read import read_store_if_written
from baibai_engine.market import price_models
from baibai_engine.market.jquants_decode import JQuantsProviderError
from baibai_engine.market.price_basis import asof_basis_prices
from baibai_engine.storage.observations.read import connect_read_only


def read_price_bars(
    sqlite_path: Path,
    *,
    tickers: Sequence[str] | None = None,
    start: date,
    end: date,
) -> dict[str, list[price_models.PriceBar]]:
    if tickers == ():
        return {}
    clause = ""
    parameters: tuple[object, ...] = (start.isoformat(), end.isoformat())
    if tickers is not None:
        clause = " AND ticker IN (" + ",".join("?" for _ in tickers) + ")"
        parameters += tuple(tickers)
    query = (
        "SELECT ticker, traded_at, close, adjustment_factor, turnover_value, ex_right_type "  # nosec B608
        "FROM jquants_daily_bars WHERE traded_at >= ? AND traded_at <= ?"
        + clause
        + " ORDER BY ticker, traded_at"
    )
    return (
        read_store_if_written(
            sqlite_path, lambda: _read_adjusted_rows(sqlite_path, query, parameters, end=end)
        )
        or {}
    )


def _read_adjusted_rows(
    path: Path, query: str, parameters: Sequence[object], *, end: date
) -> dict[str, list[price_models.PriceBar]]:
    """全社のSQL行とraw中間表を二重保持せず、銘柄単位で調整する。"""
    bars: dict[str, list[price_models.PriceBar]] = {}
    dates: dict[str, date] = {}
    with closing(connect_read_only(path)) as connection:
        rows = connection.execute(query, parameters)
        for ticker, group in groupby(rows, key=lambda row: row[0]):
            entries: list[tuple[date, float | None, float | None, float | None]] = []
            for _, traded_at, close, factor, turnover, action in group:
                _require_action_factor(ticker, traded_at, factor, action)
                if not isinstance(traded_at, str):
                    continue
                day = dates.get(traded_at)
                if day is None:
                    day = date.fromisoformat(traded_at)
                    dates[traded_at] = day
                entries.append(
                    (
                        day,
                        float(close) if isinstance(close, int | float) else None,
                        float(factor) if isinstance(factor, int | float) else None,
                        float(turnover) if isinstance(turnover, int | float) else None,
                    )
                )
            if not entries:
                continue
            valued = [
                (day, close, turnover) for day, close, _, turnover in entries if close is not None
            ]
            adjusted = asof_basis_prices(
                [(day, close) for day, close, _ in valued],
                [(day, factor) for day, _, factor, _ in entries],
                asof_date=end,
            )
            bars[str(ticker)] = [
                price_models.PriceBar(traded_at=day, price=price, turnover_value=turnover)
                for (day, _, turnover), price in zip(valued, adjusted, strict=True)
            ]
    return bars


def _require_action_factor(ticker: object, day: object, factor: object, kind: object) -> None:
    """An observed action with no coefficient is corrupt input, not no adjustment."""
    if kind is not None and factor is None:
        raise JQuantsProviderError(
            f"reported corporate action has no usable adjustment factor for {ticker} on {day}"
        )
