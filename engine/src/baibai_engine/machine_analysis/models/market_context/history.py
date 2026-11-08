"""固定された価格系列の騰落。取得・基準日選択は呼出し側が所有する。"""

from __future__ import annotations

from collections.abc import Sequence
from math import isfinite


def trailing_return(prices: Sequence[float], window_bars: int) -> float | None:
    if len(prices) < window_bars + 1:
        return None
    past, current = prices[-(window_bars + 1)], prices[-1]
    if not all(isfinite(price) and price > 0 for price in (past, current)):
        return None
    value = current / past - 1
    return value if isfinite(value) else None
