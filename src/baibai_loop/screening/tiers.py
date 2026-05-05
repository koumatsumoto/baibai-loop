from __future__ import annotations

MIN_AVG_TURNOVER_OKU = 1.0

TIER_SMALL_OKU = 100
TIER_MICRO_OKU = 200
TIER_MID_OKU = 500
TIER_LARGE_OKU = 1000

# universe フィルタ閾値と "200-500 帯" tier の下限は同じ値を指すので一元管理する。
MIN_MARKET_CAP_OKU = TIER_SMALL_OKU


def position_tier(market_cap_oku: int | float | None) -> str:
    """Return the human-readable tier label for a market cap in 億円.

    None / 数値以外のときは "unknown" を返す (universe builder で min_market_cap
    フィルタが効いている前提だが、防御的に明示する)。
    """
    if not isinstance(market_cap_oku, (int, float)) or isinstance(market_cap_oku, bool):
        return "unknown"
    if market_cap_oku >= TIER_LARGE_OKU:
        return "1000+"
    if market_cap_oku >= TIER_MID_OKU:
        return "500-1000"
    if market_cap_oku >= TIER_MICRO_OKU:
        return "200-500"
    if market_cap_oku >= TIER_SMALL_OKU:
        return "100-200"
    return "below 100 (out of universe)"
