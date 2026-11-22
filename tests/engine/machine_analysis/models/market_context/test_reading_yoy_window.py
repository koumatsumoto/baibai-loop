"""A retained level cannot stand in for the historical changes being ranked."""

from datetime import date, timedelta

import pytest
from tests.helpers.indicator_store import observations, store_reader

from baibai_engine.machine_analysis.generation.runners.reading_rules import (
    DEFAULT_RULES_PATH,
    load_reading_rules,
)
from baibai_engine.machine_analysis.models.market_context.reading.compute import compute_reading
from baibai_engine.machine_analysis.models.market_context.reading.models import (
    ReadingSnapshot,
    series_payload,
)
from baibai_engine.machine_analysis.observations.economic_indicators.definitions import (
    load_definitions,
)

ASOF = date(2026, 11, 20)


def _reading(points: list[tuple[date, float]]) -> ReadingSnapshot:
    definition = load_definitions().by_id()["jp.topix"]
    return compute_reading(
        series=(definition,),
        reader=store_reader(observations(definition, points)),
        rules=load_reading_rules(DEFAULT_RULES_PATH),
        rules_revision="test",
        asof=ASOF,
    )


def _weekdays(start: date, end: date) -> list[date]:
    return [
        day
        for n in range((end - start).days + 1)
        if (day := start + timedelta(days=n)).weekday() < 5
    ]


@pytest.mark.parametrize("early_value_date", [date(2021, 10, 29), date(2021, 11, 22)])
def test_old_unpaired_level_does_not_certify_five_year_yoy_distribution(early_value_date):
    recent = _weekdays(date(2026, 11, 11), ASOF)
    assert len(recent) == 8
    partners = {day.replace(year=day.year - 1) - timedelta(days=1): 2100.0 for day in recent}
    points = [
        *partners.items(),
        *((day, 2400.0 + i * 100) for i, day in enumerate(recent)),
    ]
    without = _reading(points).series[0]
    retained = _reading([(early_value_date, 2000.0), *points]).series[0]

    assert retained.window_years == 5
    assert retained.window_observations == without.window_observations == 8
    assert retained.expected_observations is None  # No inferred market session count.
    assert retained.latest_value == without.latest_value == 3100.0
    assert retained.statistic_value == pytest.approx((3100.0 / 2100.0 - 1.0) * 100.0)
    assert retained.statistic_value == without.statistic_value
    assert without.insufficient_history
    assert retained.insufficient_history
    assert retained.percentile is retained.z_score is None
    wire = series_payload(retained)
    assert wire["statistic_value"] == retained.statistic_value
    assert wire["percentile"] is None


@pytest.mark.parametrize("history_start_year", [2020, 2021])
def test_dense_actual_yoy_can_rank_even_if_first_year_is_rolling_warmup(history_start_year):
    # A complete fixed warm-up or a provider serving exactly the configured five
    # years. The latter has no available partner for its first comparable year.
    days = _weekdays(date(history_start_year, 11, 20), ASOF)
    points = [(day, 1000.0 + i) for i, day in enumerate(days)]
    reading = _reading(points).series[0]

    assert reading.window_observations > 1000
    assert not reading.insufficient_history
    assert reading.percentile is not None
    assert reading.z_score is not None
    assert reading.statistic_value is not None
    assert reading.latest_value == points[-1][1]
    assert reading.observed_at == ASOF
