from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from baibai_engine.macro.indicators.db import ObservationRecord
from baibai_engine.macro.indicators.definitions import SeriesDefinition, load_definitions
from baibai_engine.macro.reading.compute import MIN_WINDOW_OBSERVATIONS, compute_reading
from baibai_engine.macro.reading.rules import (
    DEFAULT_RULES_PATH,
    ReadingRules,
    ReadingRulesError,
    SeriesOverride,
    ThresholdFlag,
    load_reading_rules,
    rules_revision,
)

ASOF = date(2026, 7, 24)


def _yoy_rules(series_id: str) -> ReadingRules:
    """The published defaults with one series switched to the year-on-year statistic."""

    published = load_reading_rules(DEFAULT_RULES_PATH)
    return ReadingRules(
        schema_version=published.schema_version,
        defaults=published.defaults,
        overrides={series_id: SeriesOverride(statistic="yoy")},
    )


def _monthly_ramp(months: int, *, end: date) -> list[tuple[date, float]]:
    """Month-start points rising by one a month, so the level only ever sets records."""

    last = end.year * 12 + (end.month - 1)
    return [
        (
            date((last - offset) // 12, (last - offset) % 12 + 1, 1),
            100.0 + float(months - 1 - offset),
        )
        for offset in reversed(range(months))
    ]


def _definition(
    series_id: str = "test.series",
    *,
    frequency: str = "daily",
    unit: str = "percent",
) -> SeriesDefinition:
    return SeriesDefinition(
        series_id=series_id,
        name="Test Series",
        category="rates",
        geography="world",
        frequency=frequency,
        unit=unit,
        provider="fred_csv",
        provider_series_id="TEST",
        source_id="test-source",
        source_url="https://example.com/data.csv",
    )


def _observations(
    series_id: str, points: Sequence[tuple[date, float]]
) -> tuple[ObservationRecord, ...]:
    return tuple(
        ObservationRecord(
            series_id=series_id,
            observed_at=observed_at,
            value=value,
            unit="percent",
            source_url="https://example.com/data.csv",
        )
        for observed_at, value in points
    )


def _reader(observations: tuple[ObservationRecord, ...]):  # type: ignore[no-untyped-def]
    def read(series_id: str, start: date, end: date) -> tuple[ObservationRecord, ...]:
        return tuple(
            observation
            for observation in observations
            if observation.series_id == series_id and start <= observation.observed_at <= end
        )

    return read


def _daily_points(count: int, *, end: date, step: float) -> list[tuple[date, float]]:
    return [
        (date.fromordinal(end.toordinal() - offset), 100.0 + step * (count - 1 - offset))
        for offset in reversed(range(count))
    ]


def test_reading_rules_resolve_for_every_registered_series() -> None:
    # A series must never silently get an arbitrary window: registering one without
    # a resolvable rule has to fail here rather than produce a plausible number.
    rules = load_reading_rules(DEFAULT_RULES_PATH)

    for definition in load_definitions().series:
        resolved = rules.resolve(series_id=definition.series_id, frequency=definition.frequency)
        assert resolved.percentile_window_years >= 1
        assert resolved.staleness_warn_days >= 1


def test_reading_rules_default_the_statistic_to_the_level() -> None:
    # An unlisted series keeps the level reading, so registering one never silently
    # changes what its percentile means.
    rules = load_reading_rules(DEFAULT_RULES_PATH)

    assert rules.resolve(series_id="test.unlisted", frequency="daily").statistic == "level"
    assert rules.resolve(series_id="us.nonfarm_payrolls", frequency="monthly").statistic == "yoy"


def test_reading_rules_take_the_yoy_statistic_only_where_the_level_has_no_scale() -> None:
    """Rates, ratios and diffusion indices must keep their level percentile.

    Their levels answer "is this high" on their own, and ranking their year-on-year
    change instead would replace the reading that carries the position.
    """

    rules = load_reading_rules(DEFAULT_RULES_PATH)
    definitions = {item.series_id: item for item in load_definitions().series}

    for series_id in ("jp.10y", "vix", "jp.nikkei_pbr", "us.unemployment", "jp.cpi.core_yoy"):
        definition = definitions[series_id]
        resolved = rules.resolve(series_id=series_id, frequency=definition.frequency)
        assert resolved.statistic == "level", series_id


def test_reading_rules_override_only_names_registered_series() -> None:
    rules = load_reading_rules(DEFAULT_RULES_PATH)
    registered = {definition.series_id for definition in load_definitions().series}

    assert set(rules.overrides) <= registered


def test_reading_rules_reject_a_frequency_without_defaults() -> None:
    rules = load_reading_rules(DEFAULT_RULES_PATH)

    with pytest.raises(ReadingRulesError, match="no defaults for frequency 'biweekly'"):
        rules.resolve(series_id="test.series", frequency="biweekly")


def test_rules_revision_is_the_dated_stem() -> None:
    assert rules_revision(Path("method/macro-reading/2026-07-25T000000+0900.yaml")) == (
        "2026-07-25T000000+0900"
    )


def test_threshold_flag_comparisons_bound_their_edges() -> None:
    below = ThresholdFlag(id="contraction", comparison="below", value=50.0)
    at_or_below = ThresholdFlag(id="non_positive", comparison="at_or_below", value=0.0)
    at_or_above = ThresholdFlag(id="stress", comparison="at_or_above", value=30.0)

    assert below.matches(49.9)
    assert not below.matches(50.0)
    assert at_or_below.matches(0.0)
    assert not at_or_below.matches(0.1)
    assert at_or_above.matches(30.0)
    assert not at_or_above.matches(29.9)


def _decade_of_monthly_points(end: date) -> list[tuple[date, float]]:
    """Monthly points spanning the ten-year window, rising by one each month."""
    last = end.year * 12 + (end.month - 1)
    return [
        (date((last - offset) // 12, (last - offset) % 12 + 1, 1), float(119 - offset))
        for offset in reversed(range(120))
    ]


def test_percentile_and_z_score_match_the_window_statistics() -> None:
    definition = _definition(frequency="monthly")
    points = _decade_of_monthly_points(date(2026, 7, 1))
    observations = _observations(definition.series_id, points)
    values = [value for _, value in points]

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    reading = snapshot.series[0]
    assert reading.window_observations == len(values)
    assert not reading.insufficient_history
    # An unlisted series ranks its level, so the statistic is the latest value itself.
    assert reading.statistic == "level"
    assert reading.statistic_unit == definition.unit
    assert reading.statistic_value == reading.latest_value
    # The latest value is the maximum, so every observation is at or below it.
    assert reading.percentile == 1.0
    assert reading.z_score == pytest.approx(
        (values[-1] - statistics.fmean(values)) / statistics.stdev(values)
    )


def test_percentile_ranks_the_year_on_year_change_where_the_level_only_sets_records() -> None:
    """The point of the transform: a series that keeps rising has no position in its level.

    Its level percentile is 1.0 in every reading, while the year-on-year change of a
    linear ramp falls month after month, so the same data reads as the slowest pace of
    the window instead of its highest value.
    """

    definition = _definition("test.ramp", frequency="monthly")
    points = _monthly_ramp(132, end=date(2026, 7, 1))
    observations = _observations(definition.series_id, points)

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=_yoy_rules(definition.series_id),
        rules_revision="test",
        asof=ASOF,
    )

    reading = snapshot.series[0]
    assert reading.statistic == "yoy"
    assert reading.statistic_unit == "percent"
    assert reading.statistic_value == pytest.approx(12.0 / 219.0 * 100.0)
    # The window holds 120 month-starts and each has a partner a year back, thanks to the
    # extra history the transform reads before the window opens.
    assert reading.window_observations == 120
    assert not reading.insufficient_history
    # A linear ramp's year-on-year change shrinks every month, so the latest is the lowest.
    assert reading.percentile == pytest.approx(1 / 120)
    # The level and the absolute trend are unchanged: only what is ranked moved.
    assert reading.latest_value == 231.0
    long_trend = reading.long_trend
    assert long_trend is not None
    assert long_trend.change == pytest.approx(12.0)


def test_reading_withholds_a_year_on_year_statistic_with_no_observation_a_year_back() -> None:
    # A thirteen-month change reported as year-on-year would misstate the pace, so the
    # reading withholds the rank instead of stretching the comparison.
    definition = _definition("test.gap", frequency="monthly")
    points = [
        point for point in _monthly_ramp(132, end=date(2026, 7, 1)) if point[0] != date(2025, 7, 1)
    ]
    observations = _observations(definition.series_id, points)

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=_yoy_rules(definition.series_id),
        rules_revision="test",
        asof=ASOF,
    )

    reading = snapshot.series[0]
    assert reading.statistic_value is None
    assert reading.percentile is None
    assert reading.z_score is None
    # The history covers the window, so the reason is the missing partner, not the window.
    assert not reading.insufficient_history
    assert reading.latest_value == 231.0


def test_reading_skips_a_year_on_year_point_whose_partner_is_not_positive() -> None:
    # A ratio against zero has no value to report, so that one point leaves the sample
    # rather than entering it as an infinity.
    definition = _definition("test.zero_partner", frequency="monthly")
    points = [
        (observed_at, 0.0 if observed_at == date(2024, 7, 1) else value)
        for observed_at, value in _monthly_ramp(132, end=date(2026, 7, 1))
    ]
    observations = _observations(definition.series_id, points)

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=_yoy_rules(definition.series_id),
        rules_revision="test",
        asof=ASOF,
    )

    reading = snapshot.series[0]
    assert reading.window_observations == 119
    assert reading.statistic_value is not None


def test_reading_ranks_a_yoy_series_whose_history_starts_where_the_window_opens() -> None:
    """Coverage is judged on the raw history, which the transform then thins.

    A provider serving exactly the window (a rolling ten years) would otherwise report
    insufficient history forever: its first year has no partner, and the sample would
    start one year inside the window every day.
    """

    definition = _definition("test.rolling", frequency="monthly")
    points = _monthly_ramp(120, end=date(2026, 7, 1))
    observations = _observations(definition.series_id, points)

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=_yoy_rules(definition.series_id),
        rules_revision="test",
        asof=ASOF,
    )

    reading = snapshot.series[0]
    assert not reading.insufficient_history
    assert reading.window_observations == 108
    assert reading.expected_observations == 120
    assert reading.percentile is not None


def test_reading_is_deterministic_for_the_same_inputs() -> None:
    definition = _definition()
    observations = _observations(definition.series_id, _daily_points(30, end=ASOF, step=0.5))
    arguments = {
        "series": [definition],
        "reader": _reader(observations),
        "rules": load_reading_rules(DEFAULT_RULES_PATH),
        "rules_revision": "test",
        "asof": ASOF,
    }

    assert compute_reading(**arguments) == compute_reading(**arguments)  # type: ignore[arg-type]


def test_reading_flags_insufficient_history_instead_of_ranking_a_short_series() -> None:
    # A series that starts inside the window would be ranked against its own short
    # life, which reads as an extreme; the reading must decline instead.
    definition = _definition("test.young")
    observations = _observations(definition.series_id, _daily_points(20, end=ASOF, step=1.0))

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    reading = snapshot.series[0]
    assert reading.insufficient_history
    assert reading.percentile is None
    assert reading.z_score is None
    assert reading.latest_value == 119.0


def test_reading_declines_statistics_below_the_minimum_observation_count() -> None:
    definition = _definition("test.sparse", frequency="monthly")
    # Spans the ten-year window but holds too few points to describe a distribution.
    points = [
        (date(2016 + index, 1, 1), float(index)) for index in range(MIN_WINDOW_OBSERVATIONS - 1)
    ]
    observations = _observations(definition.series_id, points)

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    assert snapshot.series[0].insufficient_history
    assert snapshot.series[0].percentile is None


def test_reading_declines_statistics_for_a_window_with_half_its_periods_missing() -> None:
    """A sparse window describes the periods it happens to hold, not the whole window.

    A hand-maintained monthly source fills recent months first, so its holes sit in the
    older half: ranking today against that sample reads as a decade-long position while
    it is really a recent one.
    """

    definition = _definition("test.sparse_monthly", frequency="monthly")
    # Ten years of month-starts, but only every other month is present.
    points = [
        point
        for index, point in enumerate(_decade_of_monthly_points(date(2026, 7, 1)))
        if index % 2 == 0
    ]
    observations = _observations(definition.series_id, points)

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    reading = snapshot.series[0]
    assert reading.window_observations == 60
    assert reading.expected_observations == 120
    assert reading.insufficient_history
    assert reading.percentile is None


def test_reading_does_not_hold_a_daily_series_to_an_implied_period_count() -> None:
    """How many days a market trades is not a property of the frequency.

    A daily series only ever holds business days, so an implied 252-per-year count would
    declare every healthy daily series insufficient.
    """

    definition = _definition("test.business_days", frequency="daily")
    points = _daily_points(200, end=ASOF, step=0.1)
    observations = _observations(definition.series_id, points)

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    assert snapshot.series[0].expected_observations is None


def test_reading_reports_staleness_against_the_asof_date() -> None:
    definition = _definition("test.stalled")
    observations = _observations(
        definition.series_id, _daily_points(30, end=date(2026, 7, 1), step=0.1)
    )

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    reading = snapshot.series[0]
    assert reading.observed_at == date(2026, 7, 1)
    assert reading.staleness_days == 23
    assert reading.stale


def test_reading_trend_anchors_on_a_date_not_an_observation_count() -> None:
    # Monthly and daily series must mean the same thing by "3 months", so the anchor
    # is the latest observation on or before the anchor date.
    definition = _definition("test.monthly", frequency="monthly")
    points = [(date(2026, month, 1), float(month)) for month in range(1, 8)]
    observations = _observations(definition.series_id, points)

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    short_trend = snapshot.series[0].short_trend
    assert short_trend is not None
    assert short_trend.anchor_observed_at == date(2026, 4, 1)
    assert short_trend.change == pytest.approx(3.0)
    assert short_trend.direction == "up"


def test_reading_omits_a_trend_the_series_does_not_reach_back_for() -> None:
    definition = _definition("test.new", frequency="monthly")
    observations = _observations(definition.series_id, [(date(2026, 7, 1), 50.0)])

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    assert snapshot.series[0].short_trend is None
    assert snapshot.series[0].long_trend is None


def test_reading_notes_a_threshold_flag_from_the_rules() -> None:
    definition = _definition("jp.pmi_manufacturing", frequency="monthly", unit="index")
    observations = _observations(definition.series_id, [(date(2026, 6, 1), 48.5)])

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    assert snapshot.series[0].flags == ("contraction",)


def test_reading_reports_a_series_with_no_observations_without_failing() -> None:
    definition = _definition("test.empty")

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(()),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    reading = snapshot.series[0]
    assert reading.latest_value is None
    assert reading.stale
    assert reading.insufficient_history
