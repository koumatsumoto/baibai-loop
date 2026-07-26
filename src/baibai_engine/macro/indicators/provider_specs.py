"""Provider capability contracts that are safe for read-only consumers to import."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from .definitions import SeriesDefinition

type RangeReplacementPolicy = Literal["none", "through_end_vintage", "all_vintages"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderSpec:
    """A provider's fetch, store, and point-in-time read capabilities."""

    name: str
    # ``http`` fetches from an external source; ``local`` computes from other
    # stored series (derived). The batch refreshes every ``http`` series before
    # any ``local`` one so a derived series reads fresh inputs.
    kind: Literal["http", "local"] = "http"
    # All-history refresh floor. ``all_history_start`` is the reproducible fixed
    # start a bulk source exposes; ``all_history_rolling_years`` derives the floor
    # from today instead (a licensed rolling window such as J-Quants Light's 5
    # years).
    all_history_start: date | None = None
    all_history_rolling_years: int | None = None
    # Store-rewrite policy for an all-history refresh. ``trim_before_first`` drops
    # observations older than the first the provider returns (FRED's licensed
    # window defines its reproducible start). ``range_replacement`` controls
    # all-history replacement: point-in-time sources delete only vintages known by
    # the requested end, while recomputable outputs discard every old vintage in
    # the range because the current formula supersedes the old observation grid.
    trim_before_first: bool = False
    range_replacement: RangeReplacementPolicy = "none"
    # A provider can opt only formulas whose observation grid is intentionally
    # replaceable into a stronger policy. Keys are provider_series_id values.
    range_replacement_overrides: Mapping[str, RangeReplacementPolicy] = field(default_factory=dict)
    # Reads clamp to observations whose vintage is on/before the read cutoff, so a
    # publish-lagged series stays point-in-time correct (J-Quants weekly flows).
    point_in_time_vintage: bool = False
    # Credential env var names required to fetch (diagnostics only; values are
    # read from the environment by the provider, never stored in the registry).
    required_env: tuple[str, ...] = field(default=())

    def range_replacement_for(self, series: SeriesDefinition) -> RangeReplacementPolicy:
        return self.range_replacement_overrides.get(
            series.provider_series_id,
            self.range_replacement,
        )


__all__ = ["ProviderSpec", "RangeReplacementPolicy"]
