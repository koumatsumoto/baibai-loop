"""Shapes of the L2 macro reading (machine-computed descriptive statistics)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

type TrendDirection = Literal["up", "down", "flat"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SeriesTrend:
    """Change from the observation anchoring a trend window to the latest one."""

    months: int
    anchor_observed_at: date
    anchor_value: float
    change: float
    direction: TrendDirection


@dataclass(frozen=True, slots=True, kw_only=True)
class SeriesReading:
    """One series' reading: where it stands, which way it moved, how old it is.

    Every statistic is optional because a series can be too young for it, and the
    reading says so rather than substituting a value computed from a shorter
    history. ``window_years`` is the effective statistical window the rules
    resolved for this series, so a reader can tell a 10-year percentile from a
    3-year one.
    """

    series_id: str
    name: str
    category: str
    geography: str
    frequency: str
    unit: str
    latest_value: float | None
    observed_at: date | None
    staleness_days: int | None
    stale: bool
    window_years: int
    window_observations: int
    insufficient_history: bool
    percentile: float | None
    z_score: float | None
    short_trend: SeriesTrend | None
    long_trend: SeriesTrend | None
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadingSnapshot:
    """Every registered series' reading for one as-of date.

    Recomputable from the L1 store and the rules revision, so it is not stored as
    a canonical artifact: consumers either recompute it or read the projection the
    daily batch exports.
    """

    asof: date
    rules_revision: str
    series: tuple[SeriesReading, ...]
