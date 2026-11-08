"""A canonical no-trade row requires six native nulls, not lost source fields."""

from datetime import date, timedelta

import pytest

from baibai_engine.machine_analysis.models.technical_analysis.price_path import observe_price_path
from baibai_engine.machine_analysis.observations.prices.jquants_normalization import (
    normalize_daily_bars,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.machine_analysis.observations.prices.price_observation import (
    classify_price_day,
    price_calendar,
)

FIELDS = ("O", "H", "L", "C", "Vo", "Va")
IDENTITY = {"Code": "13010", "Date": "2026-11-02"}
PRICED = dict(zip(FIELDS, (100, 100, 100, 100, 0, 0), strict=True))


@pytest.mark.parametrize("missing", [None, *FIELDS])
def test_absent_fields_cannot_become_a_no_trade_row(missing):
    values = {} if missing is None else {key: None for key in FIELDS if key != missing}
    result = normalize_daily_bars([{**IDENTITY, **values}])
    assert result.rows == []
    assert result.rejected_count == 1
    assert result.status == "partial"


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize(
    "bad",
    [True, False, "invalid", "", "nan", [], {}, float("nan"), float("inf"), float("-inf"), 10**400],
)
def test_invalid_native_price_values_are_not_silently_null(field, bad):
    result = normalize_daily_bars([{**IDENTITY, **PRICED, field: bad}])
    assert result.rows == []
    assert result.rejected_count == 1
    assert result.status == "partial"


@pytest.mark.parametrize("field", ["AdjFactor", "MktCap"])
@pytest.mark.parametrize("bad", [10**400, -(10**400)])
def test_overflowing_optional_price_values_keep_the_price_observation(field, bad):
    result = normalize_daily_bars(
        [{**IDENTITY, **PRICED, "AdjFactor": 1.0, "MktCap": 1000.0, field: bad}]
    )
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row[:8] == ("1301", "2026-11-02", 100.0, 100.0, 100.0, 100.0, 0.0, 0.0)
    assert row[8] == (None if field == "AdjFactor" else 1.0)
    assert row[9:12] == (None, None, None)
    assert row[12] == (None if field == "MktCap" else 1000.0)
    assert result.rejected_count == result.excluded_count == 0
    assert result.status == "ok"


@pytest.mark.parametrize(
    ("values", "state"),
    [
        (dict.fromkeys(FIELDS), "no_trade"),
        ({"C": 100}, "mixed"),
        ({**PRICED, "O": None}, "mixed"),
        (PRICED, "priced"),
        ({key: str(value) for key, value in PRICED.items()}, "priced"),
    ],
)
def test_explicit_nulls_partial_prices_and_zero_volume_keep_their_meaning(values, state):
    result = normalize_daily_bars([{**IDENTITY, **values}])
    assert result.status == "ok"
    assert result.rejected_count == 0
    assert classify_price_day(result.rows[0][2:8]) == state


@pytest.mark.parametrize(
    ("values", "complete"),
    [({}, False), (dict.fromkeys(FIELDS, "invalid"), False), (dict.fromkeys(FIELDS), True)],
)
def test_only_explicit_no_trade_can_complete_peak_nonmatch_coverage(values, complete):
    as_of = date(2026, 11, 2)
    calendar = {
        as_of - timedelta(days=i): (as_of - timedelta(days=i)).weekday() < 5 for i in range(100)
    }
    days = sorted(day for day, business in calendar.items() if business)[-61:]
    missing_day = days[30]
    raw = [
        {**IDENTITY, "Date": day.isoformat(), **(values if day == missing_day else PRICED)}
        for day in days
    ]
    projected = normalize_daily_bars(raw)
    states = {date.fromisoformat(row[1]): classify_price_day(row[2:8]) for row in projected.rows}
    bars = [
        PriceBar(date.fromisoformat(row[1]), row[5], None)
        for row in projected.rows
        if row[5] is not None
    ]
    result = observe_price_path(
        bars, calendar=price_calendar(calendar, as_of=as_of), day_states=states
    ).payload
    assert result["observations"]["G60"]["value"] == 0.0
    assert result["observations"]["G60"]["coverage"]["complete"] is complete
    assert ("G60" in result["annotation"]["evaluated_refs"]) is complete
    assert ("G60" in result["annotation"]["unavailable_refs"]) is not complete
