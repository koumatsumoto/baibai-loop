"""解決済みの指標定義。原本catalogやregistryファイルは読まない。"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SeriesDefinition:
    """Resolved L2 reading definition, bound to its independently owned source.

    Provider and source URL are resolved from Collection, not configured twice.
    reading_key selects the L2 input column or formula; it is not the
    source acquisition request contract.
    """

    series_id: str
    name: str
    category: str
    geography: str
    frequency: str
    unit: str
    provider: str
    reading_key: str
    source_id: str
    source_url: str
    priority: int = 100
    notes: str | None = None
    plausible_min: float | None = None
    plausible_max: float | None = None
    aliases: tuple[str, ...] = ()
    tradingview_symbol: str | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("plausible_min", self.plausible_min),
            ("plausible_max", self.plausible_max),
        ):
            _validate_plausible_bound(field, value)
        if (
            self.plausible_min is not None
            and self.plausible_max is not None
            and self.plausible_min > self.plausible_max
        ):
            raise ValueError("plausible_min must be less than or equal to plausible_max")


def _validate_plausible_bound(field: str, value: object) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
    ):
        raise ValueError(f"{field} must be a finite number")


@dataclass(frozen=True)
class IndicatorDefinitions:
    series: tuple[SeriesDefinition, ...]

    def by_id(self) -> dict[str, SeriesDefinition]:
        return {item.series_id: item for item in self.series}

    def search(self, query: str) -> tuple[SeriesDefinition, ...]:
        needle = query.casefold()
        if not needle:
            return self.series
        matched: list[SeriesDefinition] = []
        for item in self.series:
            haystack = " ".join(
                (item.series_id, item.name, item.category, item.geography, *item.aliases)
            ).casefold()
            if needle in haystack:
                matched.append(item)
        return tuple(matched)
