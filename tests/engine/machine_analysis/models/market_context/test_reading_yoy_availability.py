"""最新前年比の不成立を、指数・履歴不足・取得失敗から分けて伝える。"""

from dataclasses import replace
from datetime import date

import pytest
from tests.helpers.indicator_store import observations, series_definition, store_reader

from baibai_engine.cli.market_reading import _print_table
from baibai_engine.machine_analysis.generation.runners.reading_rules import (
    DEFAULT_RULES_PATH,
    load_reading_rules,
)
from baibai_engine.machine_analysis.models.market_context.reading.compute import compute_reading
from baibai_engine.machine_analysis.models.market_context.reading.models import snapshot_payload
from baibai_engine.machine_analysis.models.market_context.reading.rules import SeriesOverride
from baibai_engine.machine_analysis.observations.economic_indicators.validation import (
    UnavailableObservationError,
)


def _snapshot(points, *, frequency="monthly", statistic="yoy", unavailable=False):
    definition = replace(
        series_definition("test.availability", frequency=frequency, unit="index"),
        notes="指数水準は前年比ではない",
    )
    rules = load_reading_rules(DEFAULT_RULES_PATH).model_copy(
        update={"overrides": {definition.series_id: SeriesOverride(statistic=statistic)}}
    )
    records = tuple(
        replace(item, notes=("source observation",), quality_flags=("source_flag",))
        for item in observations(definition, points)
    )

    def reader(series_id, start, end):
        if unavailable:
            raise UnavailableObservationError("source unavailable")
        return store_reader(records)(series_id, start, end)

    return compute_reading(
        series=(definition,),
        reader=reader,
        rules=rules,
        rules_revision="test",
        asof=date(2026, 11, 16),
    )


def test_current_yoy_can_be_missing_without_short_history_and_reaches_cli(capsys):
    # Synthetic monthly history with the same missing comparison month as the
    # saved CPI reading; this fixture is not a replacement for native CPI rows.
    points = [
        (date(year, month, 1), float(100 + (year - 2015) * 12 + month))
        for year in range(2015, 2027)
        for month in range(1, 13)
        if date(year, month, 1) <= date(2026, 10, 1) and date(year, month, 1) != date(2025, 10, 1)
    ]
    snapshot = _snapshot(points)
    reading = snapshot.series[0]
    assert reading.latest_value == 242.0
    assert reading.observed_at == date(2026, 10, 1)
    assert reading.window_observations == 117
    assert reading.expected_observations == 120
    assert not reading.insufficient_history
    assert not reading.stale
    assert reading.statistic_value is reading.percentile is reading.z_score is None
    assert reading.flags == ("source_flag",)
    assert reading.notes[:2] == (
        "Series definition: 指数水準は前年比ではない",
        "source observation",
    )
    assert len(reading.notes) == 3
    assert "比較期間の条件を満たす正の前年値がない" in reading.notes[-1]
    assert "原本非公表と取得不足は、この結果だけでは区別できません" in reading.notes[-1]
    payload = snapshot_payload(snapshot)["series"][0]
    assert payload["notes"] == list(reading.notes)
    assert payload["statistic_value"] is None
    _print_table(snapshot)
    lines = capsys.readouterr().out.splitlines()
    row = dict(zip(lines[1].split("\t"), lines[2].split("\t"), strict=True))
    assert row["latest"] == "242"
    assert row["stat_value"] == row["percentile"] == row["z"] == "-"
    assert reading.notes[-1] in row["notes"]
    assert "insufficient_history" not in row["notes"]
    assert "source_unreadable" not in row["notes"]


@pytest.mark.parametrize(
    ("frequency", "latest_day", "partner_day", "partner_value", "latest_value", "available"),
    [
        ("daily", date(2026, 10, 5), date(2025, 10, 3), 100.0, 110.0, True),
        ("daily", date(2026, 10, 1), date(2025, 9, 16), 100.0, 110.0, True),
        ("daily", date(2026, 10, 1), date(2025, 9, 15), 100.0, 110.0, False),
        ("weekly", date(2026, 10, 9), date(2025, 10, 3), 100.0, 110.0, True),
        ("monthly", date(2026, 10, 1), date(2025, 10, 1), 100.0, 110.0, True),
        ("quarterly", date(2026, 10, 1), date(2025, 10, 1), 100.0, 110.0, True),
        ("monthly", date(2024, 2, 29), date(2023, 2, 28), 100.0, 110.0, True),
        ("monthly", date(2026, 10, 1), date(2025, 9, 1), 100.0, 110.0, False),
        ("quarterly", date(2026, 10, 1), date(2025, 7, 1), 100.0, 110.0, False),
        ("monthly", date(2026, 10, 1), date(2025, 11, 1), 100.0, 110.0, False),
        ("monthly", date(2026, 10, 1), date(2025, 10, 1), 0.0, 110.0, False),
        ("monthly", date(2026, 10, 1), date(2025, 10, 1), -1.0, 110.0, False),
        ("monthly", date(2026, 10, 1), date(2025, 10, 1), 100.0, 0.0, True),
    ],
)
def test_note_follows_existing_calendar_and_positive_partner_conditions(
    frequency, latest_day, partner_day, partner_value, latest_value, available
):
    reading = _snapshot(
        [(partner_day, partner_value), (latest_day, latest_value)], frequency=frequency
    ).series[0]
    assert reading.latest_value == latest_value
    assert (reading.statistic_value is not None) == available
    # Thin history alone does not say the latest change is unavailable.
    assert reading.insufficient_history
    assert len(reading.notes) == (2 if available else 3)
    if available:
        assert reading.statistic_value == pytest.approx((latest_value / partner_value - 1) * 100)
    else:
        assert reading.percentile is reading.z_score is None


@pytest.mark.parametrize("state", ["empty", "unreadable", "level", "prior_gap", "outside_window"])
def test_note_does_not_invent_current_yoy_failure_for_other_missingness(state):
    points = [] if state == "empty" else [(date(2026, 10, 1), 100.0)]
    if state == "prior_gap":
        points.insert(0, (date(2025, 10, 1), 90.0))
    if state == "outside_window":
        points = [(date(2015, 11, 1), 90.0), (date(2016, 11, 1), 100.0)]
    reading = _snapshot(
        points,
        statistic="level" if state == "level" else "yoy",
        unavailable=state == "unreadable",
    ).series[0]
    assert not any("正の前年値" in note for note in reading.notes)
    if state in {"empty", "unreadable"}:
        assert reading.latest_value is None
        assert reading.statistic_value is None
        assert reading.flags == (("source_unreadable",) if state == "unreadable" else ())
    elif state == "outside_window":
        assert reading.latest_value == 100.0
        assert reading.statistic_value is None
        assert reading.stale
    else:
        assert reading.statistic_value is not None
