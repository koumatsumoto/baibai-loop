from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from baibai_loop.market.bars import JQuantsDailyBar
from baibai_loop.market.price_walk import resolve_price_on_or_before

# J-Quants does not carry the Nikkei 225 index itself, so forward benchmark-
# relative return uses an in-universe ETF proxy. 1321 (Nomura Nikkei 225 ETF)
# is fetched by the same `get_eq_bars_daily_range` call as the holdings, keeping
# stock and benchmark on one source and price basis. It tracks the index within
# a small ETF tracking error (~0.3pt over a few weeks), so a proxy-derived
# relative return is slightly conservative versus the underlying index.
NIKKEI225_ETF_PROXY = "1321"


def _price_on_or_before(ticker: str, target: date, bars: Sequence[JQuantsDailyBar]) -> float | None:
    resolved = resolve_price_on_or_before(ticker, target, bars)
    return resolved.price if resolved is not None else None


def _benchmark_return(
    benchmark_ticker: str,
    entry_date: date,
    asof: date,
    bars: Sequence[JQuantsDailyBar],
) -> float | None:
    entry_price = _price_on_or_before(benchmark_ticker, entry_date, bars)
    eval_price = _price_on_or_before(benchmark_ticker, asof, bars)
    if entry_price is None or eval_price is None or entry_price == 0:
        return None
    return eval_price / entry_price - 1
