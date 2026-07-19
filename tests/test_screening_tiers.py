from __future__ import annotations

import pytest

from baibai_engine.screening.tiers import MIN_MARKET_CAP_OKU, TIER_SMALL_OKU, position_tier


@pytest.mark.parametrize(
    ("market_cap", "expected"),
    [
        (99, "below 100 (out of universe)"),
        (100, "100-200"),
        (199, "100-200"),
        (200, "200-500"),
        (499, "200-500"),
        (500, "500-1000"),
        (999, "500-1000"),
        (1000, "1000+"),
        (5000.0, "1000+"),
    ],
)
def test_position_tier_returns_band_label(market_cap: int | float, expected: str) -> None:
    assert position_tier(market_cap) == expected


@pytest.mark.parametrize("missing", [None, "1000", True, False])
def test_position_tier_rejects_non_numeric_with_unknown(missing: object) -> None:
    # bool は int subclass なので isinstance ガードを別途入れている。
    assert position_tier(missing) == "unknown"  # type: ignore[arg-type]


def test_min_market_cap_aliases_tier_small() -> None:
    assert MIN_MARKET_CAP_OKU == TIER_SMALL_OKU
