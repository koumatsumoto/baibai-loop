"""Compute a reading snapshot from the L1 store, deterministically.

Pure with respect to its inputs: the same store contents, rules and as-of date
always produce the same snapshot. Nothing here fetches from a provider or writes
to a store, so a reading can be recomputed for any past date without touching the
outside world.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from statistics import fmean, stdev

from baibai_engine.macro.indicators.db import ObservationRecord
from baibai_engine.macro.indicators.definitions import SeriesDefinition

from .models import ReadingSnapshot, SeriesReading, SeriesTrend, TrendDirection
from .rules import ReadingRules, ReadingStatistic, ResolvedRule, SamplingCadence, window_start

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

# Months a year-on-year change looks back, and the raw history read before the window
# opens so the transformed sample starts where the window does.
YOY_LOOKBACK_MONTHS = 12
YOY_READ_AHEAD_MONTHS = 13

# The partner of a year-on-year change is the latest observation on or before the same
# calendar date a year earlier: a few days further back for a holiday (daily) or a stamp
# cadence (weekly), but no further. Beyond this the year-earlier period is missing, and a
# thirteen-month change reported as year-on-year would misstate the pace.
MAX_YOY_PARTNER_GAP_DAYS = 380

# The unit of a year-on-year statistic, whatever the series' own unit is.
YOY_UNIT = "percent"


@dataclass(frozen=True, slots=True)
class _StatisticPoint:
    """One point of the sample the percentile and z-score are taken over."""

    observed_at: date
    value: float


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
    observations = reader(definition.series_id, _read_start(start, rule.statistic), asof)
    if not observations:
        return _empty_reading(definition, rule)
    latest = max(observations, key=lambda observation: observation.observed_at)
    sample_observations = _fold_to_sampling_cadence(
        observations,
        cadence=rule.sampling_cadence,
    )
    sample = [
        point
        for point in _statistic_points(sample_observations, statistic=rule.statistic)
        if point.observed_at >= start
    ]
    expected = _expected_observations(rule.sampling_cadence, rule.percentile_window_years)
    if (
        expected is not None
        and rule.sampling_cadence in {"monthly", "quarterly"}
        and len(sample) > expected
    ):
        # Calendar periods are represented by real observation dates. A window
        # boundary inside the first period can therefore admit one extra period;
        # keep the most recent configured number rather than overweighting it.
        sample = sample[-expected:]
    values = [point.value for point in sample]
    # A statistic exists for the latest observation only when the transform reaches it:
    # a year-on-year change needs a partner a year back, and without one the series has
    # no current position to rank.
    reaches_latest = bool(sample) and sample[-1].observed_at == latest.observed_at
    statistic_value = values[-1] if reaches_latest else None
    # The window is only a valid frame of reference when the series actually
    # spans it; a series that starts inside the window would otherwise be ranked
    # against its own short life and read as an extreme.
    first_observed_at = min(observation.observed_at for observation in observations)
    covers_window = first_observed_at <= _window_coverage_cutoff(start, asof)
    enough_points = len(values) >= MIN_WINDOW_OBSERVATIONS
    dense_enough = expected is None or len(values) >= expected * MIN_WINDOW_COVERAGE
    insufficient = not (covers_window and enough_points and dense_enough)
    ranked = None if insufficient else statistic_value
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
        statistic=rule.statistic,
        statistic_unit=_statistic_unit(rule.statistic, definition.unit),
        statistic_value=statistic_value,
        percentile=None if ranked is None else _percentile(values, ranked),
        z_score=None if ranked is None else _z_score(values, ranked),
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
        statistic=rule.statistic,
        statistic_unit=_statistic_unit(rule.statistic, definition.unit),
        statistic_value=None,
        percentile=None,
        z_score=None,
        short_trend=None,
        long_trend=None,
        flags=(),
    )


def _read_start(start: date, statistic: ReadingStatistic) -> date:
    """Where the raw read begins: earlier than the window when the transform looks back.

    A year-on-year sample needs a partner observation for its own first point, so the
    read reaches back past the window opening. Coverage is still judged against the
    window itself, so the extra history widens the transform's reach without widening
    the frame of reference the percentile claims.
    """

    if statistic == "yoy":
        return _months_before(start, YOY_READ_AHEAD_MONTHS)
    return start


def _statistic_unit(statistic: ReadingStatistic, series_unit: str) -> str:
    return YOY_UNIT if statistic == "yoy" else series_unit


def _statistic_points(
    observations: Sequence[ObservationRecord], *, statistic: ReadingStatistic
) -> tuple[_StatisticPoint, ...]:
    ascending = sorted(observations, key=lambda observation: observation.observed_at)
    if statistic == "level":
        return tuple(
            _StatisticPoint(observed_at=observation.observed_at, value=observation.value)
            for observation in ascending
        )
    return _yoy_points(ascending)


def _fold_to_sampling_cadence(
    observations: Sequence[ObservationRecord],
    *,
    cadence: SamplingCadence,
) -> tuple[ObservationRecord, ...]:
    """Use one latest observation per configured monthly or quarterly period.

    Some providers combine a long low-frequency history with a current value that
    can be fetched repeatedly inside the latest period. Ranking every stored date
    would increasingly overweight recent periods. The sampling cadence defines
    what one statistical sample point means; latest values, trends and flags keep
    reading the original observations.
    """

    if cadence in {"raw", "weekly"}:
        return tuple(observations)

    latest_by_period: dict[tuple[int, int], ObservationRecord] = {}
    for observation in observations:
        period = (
            observation.observed_at.month
            if cadence == "monthly"
            else (observation.observed_at.month - 1) // 3
        )
        key = (observation.observed_at.year, period)
        current = latest_by_period.get(key)
        if current is None or observation.observed_at > current.observed_at:
            latest_by_period[key] = observation
    return tuple(sorted(latest_by_period.values(), key=lambda item: item.observed_at))


def _yoy_points(ascending: Sequence[ObservationRecord]) -> tuple[_StatisticPoint, ...]:
    """Percent change against the observation a year earlier, where one exists.

    Points without a usable partner are left out rather than approximated: a shorter
    comparison labelled year-on-year would misstate the pace, and a non-positive partner
    has no ratio to take. The sample thins as a result, which the observation count and
    the density check then report as thin.
    """

    dates = [observation.observed_at for observation in ascending]
    points: list[_StatisticPoint] = []
    for index, observation in enumerate(ascending):
        target = _months_before(observation.observed_at, YOY_LOOKBACK_MONTHS)
        position = bisect_right(dates, target, hi=index) - 1
        if position < 0:
            continue
        partner = ascending[position]
        gap_days = (observation.observed_at - partner.observed_at).days
        if gap_days > MAX_YOY_PARTNER_GAP_DAYS or partner.value <= 0:
            continue
        points.append(
            _StatisticPoint(
                observed_at=observation.observed_at,
                value=(observation.value / partner.value - 1.0) * 100.0,
            )
        )
    return tuple(points)


def _expected_observations(frequency: str, window_years: int) -> int | None:
    per_year = PERIODS_PER_YEAR.get(frequency)
    return None if per_year is None else per_year * window_years


def _window_coverage_cutoff(start: date, asof: date) -> date:
    # A series counts as spanning the window when it starts within the first tenth
    # of it, so a provider that begins a few weeks after the window opens is not
    # reported as having insufficient history.
    span_days = (asof - start).days
    return start.fromordinal(start.toordinal() + span_days // 10)


def _percentile(values: Sequence[float], current: float) -> float:
    """Share of the window's statistic sample at or below the current statistic, in [0, 1]."""

    at_or_below = sum(1 for value in values if value <= current)
    return at_or_below / len(values)


def _z_score(values: Sequence[float], current: float) -> float | None:
    spread = stdev(values)
    if spread == 0:
        # A flat series has no scale to express distance in.
        return None
    return (current - fmean(values)) / spread


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
    "MAX_YOY_PARTNER_GAP_DAYS",
    "MIN_WINDOW_COVERAGE",
    "MIN_WINDOW_OBSERVATIONS",
    "YOY_UNIT",
    "ObservationReader",
    "compute_reading",
]
