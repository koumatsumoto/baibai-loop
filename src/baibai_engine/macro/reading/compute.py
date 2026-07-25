"""Compute a reading snapshot from the L1 store, deterministically.

Pure with respect to its inputs: the same store contents, rules and as-of date
always produce the same snapshot. Nothing here fetches from a provider or writes
to a store, so a reading can be recomputed for any past date without touching the
outside world.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from statistics import fmean, stdev

from baibai_engine.macro.indicators.db import ObservationRecord
from baibai_engine.macro.indicators.definitions import SeriesDefinition

from .models import ReadingSnapshot, SeriesReading, SeriesTrend, TrendDirection
from .rules import ReadingRules, ResolvedRule, window_start

# Reads one series' observations in ``[start, end]`` (ascending). The caller binds
# it to the L1 store, so compute never opens a connection of its own.
type ObservationReader = Callable[[str, date, date], Sequence[ObservationRecord]]

# A percentile / z-score needs enough points that it describes a distribution
# rather than a handful of readings.
MIN_WINDOW_OBSERVATIONS = 8

# Observations a frequency implies per year. A daily series has no such number — how
# many days a market trades and a provider serves is not a property of the frequency —
# so daily series are judged by the window-start rule alone.
PERIODS_PER_YEAR: Mapping[str, int] = {"weekly": 52, "monthly": 12, "quarterly": 4}

# Share of the implied periods a window must actually hold. A window with half its
# months missing still spans the years, but its holes are not spread evenly (a
# hand-maintained source fills the recent months first), so the distribution it
# describes is the recent one wearing a ten-year label.
MIN_WINDOW_COVERAGE = 0.6


def compute_reading(
    *,
    series: Sequence[SeriesDefinition],
    reader: ObservationReader,
    rules: ReadingRules,
    rules_revision: str,
    asof: date,
) -> ReadingSnapshot:
    readings = tuple(
        _read_series(definition, reader=reader, rules=rules, asof=asof)
        for definition in sorted(series, key=lambda item: item.series_id)
    )
    return ReadingSnapshot(asof=asof, rules_revision=rules_revision, series=readings)


def _read_series(
    definition: SeriesDefinition,
    *,
    reader: ObservationReader,
    rules: ReadingRules,
    asof: date,
) -> SeriesReading:
    rule = rules.resolve(series_id=definition.series_id, frequency=definition.frequency)
    start = window_start(asof, rule.percentile_window_years)
    observations = reader(definition.series_id, start, asof)
    if not observations:
        return _empty_reading(definition, rule)
    latest = max(observations, key=lambda observation: observation.observed_at)
    values = [observation.value for observation in observations]
    # The window is only a valid frame of reference when the series actually
    # spans it; a series that starts inside the window would otherwise be ranked
    # against its own short life and read as an extreme.
    first_observed_at = min(observation.observed_at for observation in observations)
    covers_window = first_observed_at <= _window_coverage_cutoff(start, asof)
    enough_points = len(values) >= MIN_WINDOW_OBSERVATIONS
    expected = _expected_observations(definition.frequency, rule.percentile_window_years)
    dense_enough = expected is None or len(values) >= expected * MIN_WINDOW_COVERAGE
    insufficient = not (covers_window and enough_points and dense_enough)
    staleness_days = (asof - latest.observed_at).days
    return SeriesReading(
        series_id=definition.series_id,
        name=definition.name,
        category=definition.category,
        geography=definition.geography,
        frequency=definition.frequency,
        unit=definition.unit,
        latest_value=latest.value,
        observed_at=latest.observed_at,
        staleness_days=staleness_days,
        stale=staleness_days > rule.staleness_warn_days,
        staleness_warn_days=rule.staleness_warn_days,
        window_years=rule.percentile_window_years,
        window_observations=len(values),
        expected_observations=expected,
        insufficient_history=insufficient,
        percentile=None if insufficient else _percentile(values, latest.value),
        z_score=None if insufficient else _z_score(values, latest.value),
        short_trend=_trend(observations, months=rule.short_trend_months, latest=latest, asof=asof),
        long_trend=_trend(observations, months=rule.long_trend_months, latest=latest, asof=asof),
        flags=rule.matched_flags(latest.value),
    )


def _empty_reading(definition: SeriesDefinition, rule: ResolvedRule) -> SeriesReading:
    return SeriesReading(
        series_id=definition.series_id,
        name=definition.name,
        category=definition.category,
        geography=definition.geography,
        frequency=definition.frequency,
        unit=definition.unit,
        latest_value=None,
        observed_at=None,
        staleness_days=None,
        stale=True,
        staleness_warn_days=rule.staleness_warn_days,
        window_years=rule.percentile_window_years,
        window_observations=0,
        expected_observations=None,
        insufficient_history=True,
        percentile=None,
        z_score=None,
        short_trend=None,
        long_trend=None,
        flags=(),
    )


def _expected_observations(frequency: str, window_years: int) -> int | None:
    per_year = PERIODS_PER_YEAR.get(frequency)
    return None if per_year is None else per_year * window_years


def _window_coverage_cutoff(start: date, asof: date) -> date:
    # A series counts as spanning the window when it starts within the first tenth
    # of it, so a provider that begins a few weeks after the window opens is not
    # reported as having insufficient history.
    span_days = (asof - start).days
    return start.fromordinal(start.toordinal() + span_days // 10)


def _percentile(values: Sequence[float], latest: float) -> float:
    """Share of window observations at or below the latest value, in [0, 1]."""

    at_or_below = sum(1 for value in values if value <= latest)
    return at_or_below / len(values)


def _z_score(values: Sequence[float], latest: float) -> float | None:
    spread = stdev(values)
    if spread == 0:
        # A flat series has no scale to express distance in.
        return None
    return (latest - fmean(values)) / spread


def _trend(
    observations: Sequence[ObservationRecord],
    *,
    months: int,
    latest: ObservationRecord,
    asof: date,
) -> SeriesTrend | None:
    """Change from the latest observation on/before the anchor date to the latest one.

    Anchoring on a date rather than a number of observations keeps the meaning the
    same across frequencies, and returns nothing when the series does not reach
    back that far instead of comparing against its own first reading.
    """

    anchor_date = _months_before(asof, months)
    candidates = [
        observation for observation in observations if observation.observed_at <= anchor_date
    ]
    if not candidates:
        return None
    anchor = max(candidates, key=lambda observation: observation.observed_at)
    if anchor.observed_at == latest.observed_at:
        return None
    change = latest.value - anchor.value
    return SeriesTrend(
        months=months,
        anchor_observed_at=anchor.observed_at,
        anchor_value=anchor.value,
        change=change,
        direction=_direction(change),
    )


def _direction(change: float) -> TrendDirection:
    if change > 0:
        return "up"
    if change < 0:
        return "down"
    return "flat"


def _months_before(value: date, months: int) -> date:
    total = value.year * 12 + (value.month - 1) - months
    year, month = divmod(total, 12)
    day = min(value.day, _days_in_month(year, month + 1))
    return date(year, month + 1, day)


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        return (date(year + 1, 1, 1) - date(year, 12, 1)).days
    return (date(year, month + 1, 1) - date(year, month, 1)).days


__all__ = [
    "MIN_WINDOW_COVERAGE",
    "MIN_WINDOW_OBSERVATIONS",
    "ObservationReader",
    "compute_reading",
]
