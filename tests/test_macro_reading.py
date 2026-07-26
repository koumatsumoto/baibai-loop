from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest

from baibai_engine.macro.indicators.db import ObservationRecord
from baibai_engine.macro.indicators.definitions import SeriesDefinition, load_definitions
from baibai_engine.macro.reading.compute import MIN_WINDOW_OBSERVATIONS, compute_reading
from baibai_engine.macro.reading.models import SeriesReading
from baibai_engine.macro.reading.rules import (
    DEFAULT_RULES_PATH,
    PUBLICATION_INTERVAL_DAYS,
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
    category: str = "rates",
    frequency: str = "daily",
    unit: str = "percent",
) -> SeriesDefinition:
    return SeriesDefinition(
        series_id=series_id,
        name="Test Series",
        category=category,
        geography="world",
        frequency=frequency,
        unit=unit,
        provider="fred_csv",
        provider_series_id="TEST",
        source_id="test-source",
        source_url="https://example.com/data.csv",
    )


def _expects_growth_statistic(definition: SeriesDefinition) -> bool:
    """Whether registry semantics make an unscaled level percentile misleading."""

    if definition.category == "equity-index":
        return True
    if definition.category == "inflation" and definition.unit == "index":
        return True
    if definition.category == "monetary":
        return True
    if definition.category == "labor":
        return definition.unit == "index" or definition.unit.endswith(("persons", "-per-hour"))
    if definition.category == "activity":
        return definition.unit == "index" or definition.unit.endswith(
            ("-million", "-billion", "-100m")
        )
    return False


def _statistic_assignment_drift(
    definitions: Sequence[SeriesDefinition],
    rules: ReadingRules,
) -> tuple[set[str], set[str], set[str]]:
    candidates = {
        definition.series_id for definition in definitions if _expects_growth_statistic(definition)
    }
    explicit_level = {
        series_id
        for series_id, override in rules.overrides.items()
        if override.statistic == "level"
    }
    actual_yoy = {
        definition.series_id
        for definition in definitions
        if rules.resolve(
            series_id=definition.series_id,
            frequency=definition.frequency,
        ).statistic
        == "yoy"
    }
    expected_yoy = candidates - explicit_level
    return (
        expected_yoy - actual_yoy,
        actual_yoy - expected_yoy,
        explicit_level - candidates,
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
    assert rules.schema_version == 2

    for definition in load_definitions().series:
        resolved = rules.resolve(series_id=definition.series_id, frequency=definition.frequency)
        assert resolved.percentile_window_years >= 1
        assert resolved.publication_cadence in PUBLICATION_INTERVAL_DAYS
        assert resolved.publication_lag_days is not None
        assert resolved.staleness_margin_days is not None
        assert not resolved.explicit_staleness_warn_days
        assert resolved.staleness_warn_days >= 1


def test_current_rules_derive_staleness_after_the_next_print() -> None:
    rules = load_reading_rules(DEFAULT_RULES_PATH)

    for definition in load_definitions().series:
        rule = rules.resolve(
            series_id=definition.series_id,
            frequency=definition.frequency,
        )
        assert rule.publication_lag_days is not None
        assert rule.staleness_margin_days is not None
        next_print = rule.next_print_estimate(ASOF)
        assert next_print is not None
        assert rule.stale_after(ASOF) == (next_print + date.resolution * rule.staleness_margin_days)


@pytest.mark.parametrize(
    "rules_path",
    [
        path
        for path in sorted(DEFAULT_RULES_PATH.parent.glob("*.yaml"))
        if load_reading_rules(path)
        .resolve(series_id="test.legacy", frequency="monthly")
        .publication_lag_days
        is None
    ],
    ids=lambda path: path.stem,
)
def test_historical_rules_load_and_omit_the_forward_print_estimate(rules_path: Path) -> None:
    historical = load_reading_rules(rules_path)
    assert historical.schema_version == 1
    assert historical.resolve(
        series_id="test.legacy",
        frequency="monthly",
    ).explicit_staleness_warn_days
    definition = _definition(frequency="monthly")
    observed_at = date(2026, 6, 1)

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(_observations(definition.series_id, [(observed_at, 1.0)])),
        rules=historical,
        rules_revision="historical",
        asof=ASOF,
    )

    assert snapshot.series[0].next_print_estimate is None
    assert snapshot.series[0].print_due_in_days is None


def test_reading_rules_default_the_statistic_to_the_level() -> None:
    # An unlisted series keeps the level reading, so registering one never silently
    # changes what its percentile means.
    rules = load_reading_rules(DEFAULT_RULES_PATH)

    assert rules.resolve(series_id="test.unlisted", frequency="daily").statistic == "level"
    assert rules.resolve(series_id="us.nonfarm_payrolls", frequency="monthly").statistic == "yoy"


def test_reading_rules_statistic_assignments_match_registry_semantics() -> None:
    rules = load_reading_rules(DEFAULT_RULES_PATH)

    missing_yoy, unexpected_yoy, stale_level_exceptions = _statistic_assignment_drift(
        load_definitions().series,
        rules,
    )

    assert missing_yoy == set()
    assert unexpected_yoy == set()
    assert stale_level_exceptions == set()


def test_reading_rules_detect_an_unclassified_equity_index() -> None:
    rules = load_reading_rules(DEFAULT_RULES_PATH)
    added = _definition(
        "test.unlisted_equity",
        category="equity-index",
        unit="index",
    )

    missing_yoy, unexpected_yoy, stale_level_exceptions = _statistic_assignment_drift(
        (*load_definitions().series, added),
        rules,
    )

    assert missing_yoy == {"test.unlisted_equity"}
    assert unexpected_yoy == set()
    assert stale_level_exceptions == set()


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


def test_reading_rules_reject_a_series_overridden_twice(tmp_path: Path) -> None:
    """A series is overridden for unrelated reasons, so a second entry is an easy edit.

    Plain YAML keeps the last mapping and drops the settings above it, which then reads
    as a rule that was applied.
    """

    path = tmp_path / "duplicate.yaml"
    path.write_text(
        "schema_version: 1\n"
        "defaults:\n"
        "  monthly:\n"
        "    percentile_window_years: 10\n"
        "    short_trend_months: 3\n"
        "    long_trend_months: 12\n"
        "    staleness_warn_days: 100\n"
        "overrides:\n"
        "  jp.hourly_earnings:\n"
        "    staleness_warn_days: 160\n"
        "  jp.hourly_earnings:\n"
        "    statistic: yoy\n",
        encoding="utf-8",
    )

    with pytest.raises(ReadingRulesError, match="duplicate YAML mapping key"):
        load_reading_rules(path)


@pytest.mark.parametrize(
    ("schema_version", "default_fields", "override_fields", "message"),
    [
        (
            1,
            "    staleness_warn_days: 100\n"
            "    publication_lag_days: 45\n"
            "    staleness_margin_days: 7\n",
            "",
            "schema_version 1 defaults.*cannot declare",
        ),
        (
            2,
            "    publication_lag_days: 45\n",
            "",
            "schema_version 2 defaults.*require both",
        ),
        (
            2,
            "    publication_lag_days: 45\n    staleness_margin_days: 7\n",
            "  test.series:\n    staleness_warn_days: 999\n",
            "schema_version 2 override.*cannot declare staleness_warn_days",
        ),
    ],
)
def test_reading_rules_reject_mixed_or_incomplete_publication_contracts(
    tmp_path: Path,
    schema_version: int,
    default_fields: str,
    override_fields: str,
    message: str,
) -> None:
    path = tmp_path / "invalid-publication-contract.yaml"
    path.write_text(
        f"schema_version: {schema_version}\n"
        "defaults:\n"
        "  monthly:\n"
        "    percentile_window_years: 10\n"
        "    short_trend_months: 3\n"
        "    long_trend_months: 12\n"
        f"{default_fields}"
        + ("overrides:\n" + override_fields if override_fields else "overrides: {}\n"),
        encoding="utf-8",
    )

    with pytest.raises(ReadingRulesError, match=message):
        load_reading_rules(path)


# Days since the latest observation, for a series that is waiting for its next release
# and for one whose source has published since without the store catching up. The pairs
# come from each source's publication calendar: the normal age is the source's lag plus
# one publication interval, and the stopped age adds one more interval.
_STALENESS_CASES: tuple[tuple[str, int, int], ...] = (
    # OECD MEI republishes the Japanese wage index about 3.5 months late.
    ("jp.hourly_earnings", 146, 177),
    # The BOJ consumption index for month M lands in the middle of M+2.
    ("jp.real_consumption", 106, 137),
    # JOLTS publishes month M in the first week of M+2.
    ("us.jolts_openings", 97, 128),
    # OECD relays the Japanese labour force survey a few days after its own release.
    ("jp.unemployment", 100, 131),
    # Michigan publishes the preliminary reading mid-month, so waiting is short.
    ("us.consumer_sentiment", 56, 87),
    # FRED serves the EIA daily prices in weekly batches.
    ("wti", 10, 17),
    # A series on the monthly default: published mid-M+1, so the wait peaks near 76 days.
    ("us.cpi.headline", 76, 107),
)


@pytest.mark.parametrize(
    ("series_id", "waiting_days", "stopped_days"),
    _STALENESS_CASES,
    ids=[case[0] for case in _STALENESS_CASES],
)
def test_staleness_warns_only_once_a_publication_has_been_missed(
    series_id: str, waiting_days: int, stopped_days: int
) -> None:
    definition = load_definitions().by_id()[series_id]
    rules = load_reading_rules(DEFAULT_RULES_PATH)

    def reading_at(age_days: int) -> bool:
        observed_at = date.fromordinal(ASOF.toordinal() - age_days)
        snapshot = compute_reading(
            series=[definition],
            reader=_reader(_observations(series_id, [(observed_at, 1.0)])),
            rules=rules,
            rules_revision="test",
            asof=ASOF,
        )
        return snapshot.series[0].stale

    assert not reading_at(waiting_days), "normal publication waiting must not warn"
    assert reading_at(stopped_days), "a missed publication must warn"


@pytest.mark.parametrize(
    ("series_id", "observed_at", "expected_print", "expected_due_days"),
    [
        ("us.cpi.headline", date(2026, 6, 1), date(2026, 8, 17), 24),
        ("us.consumer_sentiment", date(2026, 6, 1), date(2026, 7, 29), 5),
        ("us.jolts_openings", date(2026, 5, 1), date(2026, 8, 6), 13),
        ("jp.hourly_earnings", date(2026, 3, 1), date(2026, 7, 27), 3),
        ("wti", date(2026, 7, 13), date(2026, 7, 23), -1),
        ("usd_cny", date(2026, 7, 17), date(2026, 7, 27), 3),
        ("us.erp", date(2026, 7, 24), date(2026, 7, 27), 3),
        ("us.fed_assets", date(2026, 7, 22), date(2026, 7, 30), 6),
        ("us.tga", date(2026, 7, 22), date(2026, 7, 30), 6),
        ("us.net_liquidity", date(2026, 7, 22), date(2026, 7, 30), 6),
        ("jp.monetary_base", date(2026, 6, 1), date(2026, 8, 3), 10),
    ],
)
def test_next_print_estimate_uses_the_series_publication_lag(
    series_id: str,
    observed_at: date,
    expected_print: date,
    expected_due_days: int,
) -> None:
    definition = load_definitions().by_id()[series_id]
    snapshot = compute_reading(
        series=[definition],
        reader=_reader(_observations(series_id, [(observed_at, 1.0)])),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    reading = snapshot.series[0]
    assert reading.next_print_estimate == expected_print
    assert reading.print_due_in_days == expected_due_days


def test_publication_cadence_can_differ_from_registry_frequency() -> None:
    definition = load_definitions().by_id()["us.erp"]
    rule = load_reading_rules(DEFAULT_RULES_PATH).resolve(
        series_id=definition.series_id,
        frequency=definition.frequency,
    )

    assert definition.frequency == "monthly"
    assert rule.sampling_cadence == "monthly"
    assert rule.publication_cadence == "business_daily"
    assert rule.staleness_warn_days == 7


def test_month_end_print_estimate_advances_by_a_calendar_month() -> None:
    definition = _definition(frequency="monthly")
    observed_at = date(2026, 1, 31)
    snapshot = compute_reading(
        series=[definition],
        reader=_reader(_observations(definition.series_id, [(observed_at, 1.0)])),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=date(2026, 2, 1),
    )

    assert snapshot.series[0].next_print_estimate == date(2026, 4, 14)


def test_month_end_staleness_uses_the_same_calendar_boundary_as_print_due() -> None:
    definition = _definition(frequency="monthly")
    observed_at = date(2026, 1, 31)
    rules = load_reading_rules(DEFAULT_RULES_PATH)

    def reading_at(asof: date) -> SeriesReading:
        return compute_reading(
            series=[definition],
            reader=_reader(_observations(definition.series_id, [(observed_at, 1.0)])),
            rules=rules,
            rules_revision="test",
            asof=asof,
        ).series[0]

    margin_last_day = reading_at(date(2026, 4, 21))
    margin_exceeded = reading_at(date(2026, 4, 22))
    assert margin_last_day.next_print_estimate == date(2026, 4, 14)
    assert margin_last_day.staleness_warn_days == 80
    assert not margin_last_day.stale
    assert margin_exceeded.stale


def test_daily_print_estimate_skips_a_weekend_without_an_event_calendar() -> None:
    definition = _definition(frequency="daily")
    observed_at = date(2026, 7, 24)  # Friday
    snapshot = compute_reading(
        series=[definition],
        reader=_reader(_observations(definition.series_id, [(observed_at, 1.0)])),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=date(2026, 7, 26),
    )

    reading = snapshot.series[0]
    assert reading.next_print_estimate == date(2026, 7, 27)
    assert reading.print_due_in_days == 1


def test_calendar_daily_print_estimate_keeps_weekend_observations() -> None:
    definition = load_definitions().by_id()["btc_usd"]
    observed_at = date(2026, 7, 24)  # Friday
    snapshot = compute_reading(
        series=[definition],
        reader=_reader(_observations(definition.series_id, [(observed_at, 1.0)])),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=observed_at,
    )

    reading = snapshot.series[0]
    assert reading.next_print_estimate == date(2026, 7, 25)
    assert reading.print_due_in_days == 1


def test_every_registered_series_gets_a_forward_print_estimate() -> None:
    definitions = load_definitions().series
    observations = {
        definition.series_id: ObservationRecord(
            series_id=definition.series_id,
            observed_at=ASOF,
            value=1.0,
            unit=definition.unit,
            source_url=definition.source_url,
        )
        for definition in definitions
    }

    def read(series_id: str, start: date, end: date) -> tuple[ObservationRecord, ...]:
        observation = observations[series_id]
        return (observation,) if start <= observation.observed_at <= end else ()

    snapshot = compute_reading(
        series=definitions,
        reader=read,
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    assert len(definitions) >= 109
    assert len(snapshot.series) == len(definitions)
    assert all(reading.next_print_estimate is not None for reading in snapshot.series)
    assert all(reading.print_due_in_days is not None for reading in snapshot.series)
    rules = load_reading_rules(DEFAULT_RULES_PATH)
    definitions_by_id = {definition.series_id: definition for definition in definitions}
    for reading in snapshot.series:
        assert reading.next_print_estimate is not None
        definition = definitions_by_id[reading.series_id]
        rule = rules.resolve(
            series_id=definition.series_id,
            frequency=definition.frequency,
        )
        if rule.publication_cadence != "calendar_daily":
            assert reading.next_print_estimate.weekday() < 5


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


def test_monthly_reading_counts_one_statistic_point_per_calendar_month() -> None:
    definition = _definition("test.mixed_monthly", frequency="monthly")
    points = [
        (date(2016, 7, 29), -1.0),
        *_decade_of_monthly_points(date(2026, 7, 1)),
        (date(2026, 7, 2), 120.0),
        (date(2026, 7, 24), 121.0),
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
    assert reading.window_observations == 120
    assert reading.expected_observations == 120
    assert reading.latest_value == 121.0
    assert reading.statistic_value == 121.0
    assert reading.percentile == 1.0


def test_daily_source_can_use_a_monthly_statistic_sample() -> None:
    definition = _definition("test.daily_source_monthly_sample", frequency="daily")
    points = [
        *_decade_of_monthly_points(date(2026, 7, 1)),
        (date(2026, 7, 2), 120.0),
        (date(2026, 7, 24), 121.0),
    ]
    rules = load_reading_rules(DEFAULT_RULES_PATH).model_copy(
        update={
            "overrides": {
                definition.series_id: SeriesOverride(sampling_cadence="monthly"),
            }
        }
    )

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(_observations(definition.series_id, points)),
        rules=rules,
        rules_revision="test",
        asof=ASOF,
    )

    reading = snapshot.series[0]
    assert reading.frequency == "daily"
    assert reading.window_observations == 120
    assert reading.expected_observations == 120
    assert reading.latest_value == 121.0


def test_reading_rules_reject_an_unsupported_weekly_sampling_override() -> None:
    with pytest.raises(ValueError, match="sampling_cadence"):
        SeriesOverride(sampling_cadence="weekly")  # type: ignore[arg-type]


def test_quarterly_reading_counts_one_statistic_point_per_calendar_quarter() -> None:
    definition = _definition("test.mixed_quarterly", frequency="quarterly")
    last_quarter = 2026 * 4 + 2
    points = [
        (
            date((last_quarter - offset) // 4, ((last_quarter - offset) % 4) * 3 + 1, 1),
            float(39 - offset),
        )
        for offset in reversed(range(40))
    ]
    points.extend(((date(2026, 7, 2), 40.0), (date(2026, 7, 24), 41.0)))
    observations = _observations(definition.series_id, points)

    snapshot = compute_reading(
        series=[definition],
        reader=_reader(observations),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )

    reading = snapshot.series[0]
    assert reading.window_observations == 40
    assert reading.expected_observations == 40
    assert reading.statistic_value == 41.0


def test_daily_reading_keeps_every_stored_statistic_point() -> None:
    definition = _definition("test.mixed_daily", frequency="daily")
    points = [
        *_decade_of_monthly_points(date(2026, 7, 1)),
        (date(2026, 7, 2), 120.0),
        (date(2026, 7, 24), 121.0),
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
    assert reading.window_observations == 122
    assert reading.expected_observations is None


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
    assert reading.next_print_estimate == date(2026, 7, 2)
    assert reading.print_due_in_days == -22


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
    assert reading.next_print_estimate is None
    assert reading.print_due_in_days is None
