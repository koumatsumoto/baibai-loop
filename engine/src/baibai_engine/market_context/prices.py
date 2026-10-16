"""価格観測の読取と、保存した価格調整係数に基づく価格のみの比較算術。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.foundation.sqlite_read import read_rows
from baibai_engine.market.bars import (
    JQuantsAdjustmentFactorEvent,
    JQuantsDailyBar,
    asof_basis_closes,
    asof_basis_prices,
)
from baibai_engine.storage.observations.read import connect_read_only


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
        connector=connect_read_only,
    )
    # 取得世代の異なる調整済み価格を混ぜず、価格factorの後方累積で末尾基準へ揃える。
    # factor自体にも配信元の訂正があるため、原本の再取得時には訂正値を保存する。
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


# How far before the requested start a bar may sit: the market is closed for up to
# a week around the New Year, so a shorter window would drop the comparison端.
_CHANGE_START_LOOKBACK_DAYS = 15


def close_change_since(path: Path, tickers: Sequence[str], *, since: date) -> dict[str, float]:
    """Return each ticker's percent close change from ``since`` to its latest close.

    Both ends are put on the latest price-adjustment basis before dividing. The stored close is
    unadjusted and provider-adjusted prices mix vintages across incremental acquisition, so
    a split between the two dates would otherwise read as a price move of the split
    ratio — a 1:2 split as a 50% fall. The correction uses the stored
    ``adjustment_factor``. The coefficient and the ex-rights event kind are distinct;
    the coefficient alone does not establish a share-unit change. Provider corrections
    can change previously acquired coefficients.

    A ticker with no close at or before ``since`` yields no entry: there is nothing to
    compare against, which is different from having not moved.
    """

    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    by_ticker, events_by_ticker = _read_price_observations(
        path, unique, start=since - timedelta(days=_CHANGE_START_LOOKBACK_DAYS)
    )
    changes: dict[str, float] = {}
    for ticker, bars in by_ticker.items():
        start = _index_on_or_before(bars, since)
        if start is None or start == len(bars) - 1:
            continue
        closes = asof_basis_closes(
            bars,
            events_by_ticker.get(ticker, ()),
            asof_date=bars[-1].traded_at,
        )
        if closes[start] == 0:
            continue
        changes[ticker] = round((closes[-1] / closes[start] - 1) * 100, 1)
    return changes


def _index_on_or_before(bars: Sequence[JQuantsDailyBar], day: date) -> int | None:
    for index in range(len(bars) - 1, -1, -1):
        if bars[index].traded_at <= day:
            return index
    return None


def worst_close_drawdown(
    path: Path, tickers: Sequence[str], *, start: date, end: date
) -> dict[str, float]:
    """Return each ticker's deepest close-to-close fall from ``start`` within the window.

    This measures temporary price downside from the entry, not permanent business
    impairment or peak-to-trough drawdown. A name that halved and recovered reads
    as flat at the endpoint but still has an entry-relative trough. Both ends use
    the window's latest price-adjustment basis so an action is not a price fall.

    The value is a ratio (``-0.3`` for a 30% trough) and is never positive: a name
    that stayed above its entry did not fall. A ticker with no close at or before
    ``start`` yields no entry.
    """

    if not tickers:
        return {}
    unique = list(dict.fromkeys(tickers))
    by_ticker, events_by_ticker = _read_price_observations(
        path, unique, start=start - timedelta(days=_CHANGE_START_LOOKBACK_DAYS), end=end
    )
    worst: dict[str, float] = {}
    for ticker, bars in by_ticker.items():
        entry = _index_on_or_before(bars, start)
        if entry is None or entry == len(bars) - 1:
            continue
        closes = asof_basis_closes(
            bars,
            events_by_ticker.get(ticker, ()),
            asof_date=end,
        )
        if closes[entry] == 0:
            continue
        trough = min(closes[entry + 1 :])
        # A name that never traded below its entry has no drawdown. Reporting the
        # distance to its lowest point would call a rise a fall.
        worst[ticker] = min(0.0, trough / closes[entry] - 1)
    return worst


def _read_price_observations(
    path: Path,
    tickers: Sequence[str],
    *,
    start: date,
    end: date | None = None,
) -> tuple[dict[str, list[JQuantsDailyBar]], dict[str, list[JQuantsAdjustmentFactorEvent]]]:
    """Read valued bars and independent action events from the same SQL snapshot."""
    placeholders = ",".join("?" for _ in tickers)
    end_clause = " AND traded_at <= ?" if end is not None else ""
    parameters: list[object] = [*tickers, start.isoformat()]
    if end is not None:
        parameters.append(end.isoformat())
    rows = read_rows(
        path,
        f"""
            SELECT ticker, traded_at, close, adjustment_factor
            FROM jquants_daily_bars
            WHERE ticker IN ({placeholders})
              AND traded_at >= ?
              {end_clause}
            ORDER BY ticker, traded_at
            """,  # nosec B608
        parameters,
        connector=connect_read_only,
    )
    bars: dict[str, list[JQuantsDailyBar]] = {}
    events: dict[str, list[JQuantsAdjustmentFactorEvent]] = {}
    for ticker, traded_at, close, factor in rows:
        if close is not None:
            bars.setdefault(str(ticker), []).append(
                JQuantsDailyBar(
                    ticker=str(ticker),
                    traded_at=date.fromisoformat(str(traded_at)),
                    close=float(close),
                    turnover_value=None,
                    adjustment_factor=None if factor is None else float(factor),
                )
            )
        if factor not in (None, 1.0):
            events.setdefault(str(ticker), []).append(
                JQuantsAdjustmentFactorEvent(
                    ticker=str(ticker),
                    traded_at=date.fromisoformat(str(traded_at)),
                    adjustment_factor=float(factor),
                )
            )
    return bars, events
