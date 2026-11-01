"""固定された価格系列の騰落。取得・基準日選択は呼出し側が所有する。"""

from __future__ import annotations

from collections.abc import Sequence


def trailing_return(prices: Sequence[float], window_bars: int) -> float | None:
    if len(prices) < window_bars + 1:
        return None
    past = prices[-(window_bars + 1)]
    if past == 0:
        return None
    return prices[-1] / past - 1
