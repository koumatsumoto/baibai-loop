"""Screening比較母集団の業種平均対市場平均。snapshotの中央値とは別の量。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True, slots=True)
class SectorRelativeStrength:
    mean_returns: dict[str, float]
    relative_returns: dict[str, float | None]
    percentiles: dict[str, float | None]


def sector_relative_strength(
    returns_by_ticker: Mapping[str, float], sectors_by_ticker: Mapping[str, str]
) -> SectorRelativeStrength:
    """Caller supplies the unchanged comparison population and price-return window."""
    grouped: dict[str, list[float]] = {}
    for ticker, value in returns_by_ticker.items():
        grouped.setdefault(sectors_by_ticker[ticker], []).append(value)
    market_return = mean(returns_by_ticker.values()) if returns_by_ticker else None
    means = {sector: mean(values) for sector, values in grouped.items()}
    relative = {
        sector: value - market_return if market_return is not None else None
        for sector, value in means.items()
    }
    return SectorRelativeStrength(means, relative, _rank_to_percentiles(relative))


def _rank_to_percentiles(values: Mapping[str, float | None]) -> dict[str, float | None]:
    available = sorted((value, key) for key, value in values.items() if value is not None)
    if not available:
        return dict.fromkeys(values)
    total = len(available)
    output: dict[str, float | None] = dict.fromkeys(values)
    for index, (_, key) in enumerate(available, start=1):
        output[key] = index / total
    return output
