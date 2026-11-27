"""銘柄別の調整係数と一括/scoped読取の一致を検証する。"""

from contextlib import closing
from datetime import date, timedelta
from math import inf, nextafter

import pytest
from tests.helpers.price_native import project_price_records

from baibai_engine.machine_analysis.models.technical_analysis.price_path import observe_price_path
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.machine_analysis.observations.prices.price_observation import price_calendar
from baibai_engine.machine_analysis.observations.prices.price_observation_reader import (
    read_price_day_inputs,
)
from baibai_engine.machine_analysis.observations.prices.price_reader import read_ticker_bars
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def test_streamed_companies_keep_independent_adjustments_and_null_close_actions(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as connection:
        connection.executemany(
            "INSERT INTO jquants_daily_bars"
            "(ticker,traded_at,close,adjustment_factor,turnover_value) VALUES (?,?,?,?,?)",
            [
                ("1111", "2026-01-05", 100.0, 1.0, 1000.0),
                ("1111", "2026-01-06", None, 0.5, None),
                ("1111", "2026-01-07", 60.0, 1.0, 600.0),
                ("2222", "2026-01-05", 200.0, 1.0, 2000.0),
                ("2222", "2026-01-07", 220.0, 1.0, 2200.0),
            ],
        )
        connection.commit()
    all_bars = read_price_bars(path, start=date(2026, 1, 5), end=date(2026, 1, 7))
    assert [bar.price for bar in all_bars["1111"]] == [50.0, 60.0]
    assert [bar.price for bar in all_bars["2222"]] == [200.0, 220.0]
    assert [bar.turnover_value for bar in all_bars["1111"]] == [1000.0, 600.0]
    for ticker in all_bars:
        assert read_price_bars(
            path, tickers=(ticker,), start=date(2026, 1, 5), end=date(2026, 1, 7)
        ) == {ticker: all_bars[ticker]}


def test_decimal_splits_preserve_equal_and_adjacent_received_closes(tmp_path):
    path = tmp_path / "prices.sqlite"
    source = [
        ("2026-01-05", nextafter(1003.0, 0.0), 1.0, None),
        ("2026-01-06", 1003.0, 1.0, None),
        ("2026-01-07", nextafter(1003.0, inf), 1.0, None),
        ("2026-01-08", 501.5, 0.5, "1"),
        ("2026-01-09", 100.3, 0.2, "1"),
    ]
    project_price_records(
        path,
        [
            {
                "Code": "11110",
                "Date": day,
                **dict.fromkeys(("O", "H", "L", "C"), close),
                "Vo": 100,
                "Va": 100 * close,
                "AdjFactor": factor,
                "ExRT": action,
            }
            for day, close, factor, action in source
        ],
    )
    adjusted = read_price_bars(path, start=date(2026, 1, 5), end=date(2026, 1, 9))
    low, flat, high, after_first, current = (bar.price for bar in adjusted["1111"])
    assert flat == after_first == current == 100.3
    assert low < current < high


@pytest.mark.parametrize(
    ("wrong_close", "invalid_volume"),
    [(True, False), (False, True)],
)
def test_adjusted_price_path_excludes_only_the_contradictory_close(
    tmp_path, wrong_close, invalid_volume
):
    path = tmp_path / "prices.sqlite"
    as_of = date(2026, 1, 30)
    days = [as_of - timedelta(days=n) for n in range(40)]
    calendar = price_calendar({d: d.weekday() < 5 for d in days}, as_of=as_of)
    window = calendar.windows[20]
    assert window is not None
    event_day = window[10]
    records = []
    for day in window:
        close = 200 if day < event_day else (110 if day == as_of else 100)
        records.append(
            {
                "Code": "12340",
                "Date": day.isoformat(),
                **dict.fromkeys(("O", "H", "L", "C"), close),
                "Vo": 100,
                "Va": 123456,
                "AdjFactor": 1,
            }
        )
    records[10].update(
        {
            "C": 800 if wrong_close else 100,
            "Vo": -5 if invalid_volume else 100,
            "AdjFactor": 0.5,
            "ExRT": "1",
        }
    )
    project_price_records(path, records)
    with closing(connect_read_only(path)) as connection:
        states, turnover = read_price_day_inputs(
            connection,
            start=window[0],
            end=as_of,
            tickers=("1234",),
            turnover_start=window[0],
            turnover_tickers=("1234",),
        )
        marks = read_ticker_bars(
            connection, {window[0]: {"1234"}, event_day: {"1234"}, as_of: {"1234"}}
        )
        assert marks is not None
        assert {bar.traded_at: bar.close for bar in marks} == (
            {window[0]: 200, as_of: 110}
            if wrong_close
            else {window[0]: 200, event_day: 100, as_of: 110}
        )
        retained = connection.execute(
            "SELECT close, volume FROM jquants_daily_bars WHERE ticker='1234' AND traded_at=?",
            (event_day.isoformat(),),
        ).fetchone()
        assert tuple(retained) == (800 if wrong_close else 100, -5 if invalid_volume else 100)
    assert states["1234"][event_day] == "invalid"
    assert turnover["1234"][event_day] == 123456
    bars = read_price_bars(path, tickers=("1234",), start=window[0], end=as_of)["1234"]
    # Action survives independently of its contradictory close; raw yen values are never adjusted.
    assert (bars[0].price, bars[0].turnover_value) == (100, 123456)
    assert (event_day in {bar.traded_at for bar in bars}) is not wrong_close
    path_observation = observe_price_path(
        bars, calendar=calendar, day_states=states["1234"]
    ).payload["observations"]["R20"]
    assert path_observation["value"] == pytest.approx(0.1)  # Endpoints do not need the interior.
    assert path_observation["coverage"]["row_counts"]["invalid"] == 1
    assert path_observation["path_statistics"]["status"] == (
        "incomplete_price_window" if wrong_close else "available"
    )
