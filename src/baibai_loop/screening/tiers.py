from __future__ import annotations

MIN_MARKET_CAP_OKU = 200
MIN_AVG_TURNOVER_OKU = 3.0

TIER_SMALL_OKU = 200
TIER_MID_OKU = 500
TIER_LARGE_OKU = 1000


def position_tier(market_cap_oku: int) -> str:
    if market_cap_oku >= TIER_LARGE_OKU:
        return "1000+ (max 2.0%)"
    if market_cap_oku >= TIER_MID_OKU:
        return "500-1000 (max 1.0%)"
    if market_cap_oku >= TIER_SMALL_OKU:
        return "200-500 (P-B only, max 0.5%)"
    return "below 200 (out of universe)"
