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


def snapshot_payload(snapshot: ReadingSnapshot) -> dict[str, object]:
    """The wire form of a snapshot, shared by the CLI and the read-only API.

    One serializer keeps `macro reading --format json` and the read model byte-for-byte
    comparable, so a UI panel and a hand check are reading the same numbers.
    """

    return {
        "asof": snapshot.asof.isoformat(),
        "rules_revision": snapshot.rules_revision,
        "series": [series_payload(reading) for reading in snapshot.series],
    }


def series_payload(reading: SeriesReading) -> dict[str, object]:
    return {
        "series_id": reading.series_id,
        "name": reading.name,
        "category": reading.category,
        "geography": reading.geography,
        "frequency": reading.frequency,
        "unit": reading.unit,
        "latest_value": reading.latest_value,
        "observed_at": None if reading.observed_at is None else reading.observed_at.isoformat(),
        "staleness_days": reading.staleness_days,
        "stale": reading.stale,
        "window_years": reading.window_years,
        "window_observations": reading.window_observations,
        "insufficient_history": reading.insufficient_history,
        "percentile": reading.percentile,
        "z_score": reading.z_score,
        "short_trend": _trend_payload(reading.short_trend),
        "long_trend": _trend_payload(reading.long_trend),
        "flags": list(reading.flags),
    }


def _trend_payload(trend: SeriesTrend | None) -> dict[str, object] | None:
    if trend is None:
        return None
    return {
        "months": trend.months,
        "anchor_observed_at": trend.anchor_observed_at.isoformat(),
        "anchor_value": trend.anchor_value,
        "change": trend.change,
        "direction": trend.direction,
    }


__all__ = [
    "ReadingSnapshot",
    "SeriesReading",
    "SeriesTrend",
    "TrendDirection",
    "series_payload",
    "snapshot_payload",
]
