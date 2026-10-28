"""Technical L2 arithmetic, retained-prefix semantics, and read-only public outlets."""

import hashlib
import json
import socket
from contextlib import closing
from datetime import date, timedelta

import pytest
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.machine_analysis.models.technical_analysis.calculations import (
    bollinger_20bar,
    rsi_14bar,
    simple_moving_average,
)
from baibai_engine.machine_analysis.models.technical_analysis.cli import main
from baibai_engine.machine_analysis.models.technical_analysis.generation import generate_technical_analysis
from baibai_engine.machine_analysis.models.technical_analysis.query import technical_analysis
from baibai_engine.market.price_models import PriceBar
from baibai_engine.storage.observations.schema import SQLiteSchemaError, open_connection

ASOF = date(2026, 10, 16)


def observed(closes):
    return [
        PriceBar(ASOF - timedelta(days=len(closes) - index - 1), price, None)
        for index, price in enumerate(closes)
    ]


@pytest.mark.parametrize(
    ("closes", "value", "status"),
    [
        ([], None, "insufficient_history"),
        ([100] * 14, None, "insufficient_history"),
        ([100] * 15, 50, "available"),
        (list(range(100, 115)), 100, "available"),
        (list(range(115, 100, -1)), 0, "available"),
        (list(range(100, 115)) + [114] * 20, 100, "available"),
        (list(range(115, 100, -1)) + [101] * 20, 0, "available"),
        ([0] + [100] * 20, None, "invalid_price"),
        ([float("nan")] + [100] * 20, None, "invalid_price"),
        ([float("inf")] + [100] * 20, None, "invalid_price"),
    ],
)
def test_rsi_seed_missingness_flat_and_one_sided_history(closes, value, status):
    bars = observed(closes)
    result = rsi_14bar(bars)
    assert result["status"] == status
    assert result["value"] == (pytest.approx(value) if value is not None else None)
    assert result["bar_count"] == len(bars)
    assert result["history_start_date"] == (bars[0].traded_at.isoformat() if bars else None)
    assert result["seed_end_date"] == (bars[14].traded_at.isoformat() if len(bars) >= 15 else None)


def test_wilder_seed_then_recursion_is_not_a_trailing_simple_average():
    prices = [100]
    for _ in range(7):
        prices.extend((prices[-1] + 2, prices[-1] + 1))
    seeded = rsi_14bar(observed(prices))
    assert seeded["average_gain"] == 1
    assert seeded["average_loss"] == 0.5
    assert seeded["value"] == pytest.approx(200 / 3)
    smoothed = rsi_14bar(observed([*prices, prices[-1] - 3]))
    assert smoothed["average_gain"] == pytest.approx(13 / 14)
    assert smoothed["average_loss"] == pytest.approx(19 / 28)
    assert smoothed["value"] == pytest.approx(100 * 26 / 45)


def test_old_invalid_price_invalidates_rsi_but_not_recent_bollinger():
    bars = observed([0] + [100] * 19 + [120])
    assert rsi_14bar(bars)["status"] == "invalid_price"
    assert bollinger_20bar(bars)["status"] == "available"
    assert bollinger_20bar(bars)["z_score"] == pytest.approx(19**0.5)


def test_bollinger_band_levels_width_and_percent_b_are_not_clipped():
    flat = bollinger_20bar(observed([100] * 20))
    assert flat["sigma"] == flat["bandwidth"] == 0
    assert flat["middle_band"] == flat["upper_band"] == flat["lower_band"] == 100
    assert flat["percent_b"] is flat["z_score"] is flat["position"] is None
    outside = bollinger_20bar(observed([100] * 19 + [120]))
    assert outside["middle_band"] == 101
    assert outside["sigma"] == pytest.approx(19**0.5)
    assert outside["bandwidth"] == pytest.approx(4 * 19**0.5 / 101)
    assert outside["percent_b"] == pytest.approx(0.5 + 19**0.5 / 4)


def test_query_and_cli_read_entire_prefix_without_writes_or_network(tmp_path, monkeypatch, capsys):
    path = tmp_path / "market.sqlite"
    insert_daily_bars_from_closes(path, "1234", list(range(100, 130)), end_date=ASOF)
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    def no_network(*args, **kwargs):
        pytest.fail("technical read attempted network access")

    monkeypatch.setattr(socket, "socket", no_network)
    result = technical_analysis(path, ticker="1234", as_of=ASOF + timedelta(days=7))
    assert result["bar_count"] == 30
    assert result["history_start_date"] == (ASOF - timedelta(days=29)).isoformat()
    assert result["history_end_date"] == ASOF.isoformat()
    assert result["rsi_14bar"]["seed_end_date"] == (ASOF - timedelta(days=15)).isoformat()
    assert result["rsi_14bar"]["value"] == 100
    assert result["bollinger_20bar"]["window_bars"] == 20
    assert (
        main(
            ["show", "--ticker", "1234", "--asof", str(ASOF + timedelta(days=7)), "--db", str(path)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == result
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_missing_store_returns_missing_indicators_without_creating_a_store(tmp_path):
    path = tmp_path / "absent.sqlite"
    result = technical_analysis(path, ticker="1234", as_of=ASOF)
    assert result["bar_count"] == 0
    assert result["bollinger_20bar"]["status"] == "insufficient_history"
    assert result["rsi_14bar"]["status"] == "insufficient_history"
    assert not path.exists()


def test_shared_price_generation_uses_the_same_calculations_at_the_fixed_date():
    bars = observed(list(range(100, 140)))
    result = generate_technical_analysis(
        [PriceBar(ASOF + timedelta(days=1), 999.0, None), *reversed(bars)],
        ticker="1234",
        as_of=ASOF,
        trading_dates=(),
    )
    assert result["bar_count"] == 40
    assert result["history_end_date"] == ASOF.isoformat()
    assert result["rsi_14bar"] == rsi_14bar(bars)
    assert result["bollinger_20bar"] == bollinger_20bar(bars)
    assert result["moving_averages"]["sma_20bar"] == simple_moving_average(bars, window_bars=20)


def test_sma_requires_its_own_window_and_reports_price_distance():
    bars = observed(list(range(100, 120)))
    short = simple_moving_average(bars, window_bars=20)
    assert short["value"] == 109.5
    assert short["gap_ratio"] == pytest.approx(119 / 109.5 - 1)
    assert simple_moving_average(bars, window_bars=200)["status"] == "insufficient_history"
    assert (
        simple_moving_average(observed([100] * 19 + [0]), window_bars=20)["status"]
        == "invalid_price"
    )


def test_query_does_not_truncate_wilder_seed_to_market_context_lookback(tmp_path):
    path = tmp_path / "market.sqlite"
    insert_daily_bars_from_closes(
        path, "1234", list(range(100, 115)), end_date=ASOF - timedelta(days=1400)
    )
    insert_daily_bars_from_closes(path, "1234", list(range(115, 130)), end_date=ASOF)
    result = technical_analysis(path, ticker="1234", as_of=ASOF)
    assert result["bar_count"] == 30
    assert result["rsi_14bar"]["history_start_date"] == (ASOF - timedelta(days=1414)).isoformat()
    assert result["rsi_14bar"]["seed_end_date"] == (ASOF - timedelta(days=1400)).isoformat()
    assert result["rsi_14bar"]["value"] == 100
    assert result["bollinger_20bar"]["window_bars"] == 20
    assert (
        result["moving_averages"]["sma_20bar"]["value"] == result["bollinger_20bar"]["middle_band"]
    )
    assert result["moving_averages"]["sma_200bar"]["status"] == "insufficient_history"


def test_null_event_and_future_rows_keep_actual_observation_dates(tmp_path):
    path = tmp_path / "market.sqlite"
    insert_daily_bars_from_closes(path, "1234", [200] * 20 + [120], end_date=ASOF)
    connection = open_connection(path)
    try:
        connection.execute(
            "UPDATE jquants_daily_bars SET close=NULL, adjustment_factor=0.5 "
            "WHERE ticker='1234' AND traded_at=?",
            ((ASOF - timedelta(days=1)).isoformat(),),
        )
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close,adjustment_factor) "
            "VALUES('1234',?,9999,0.1)",
            ((ASOF + timedelta(days=1)).isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    result = technical_analysis(path, ticker="1234", as_of=ASOF)
    assert result["bar_count"] == 20
    assert result["history_end_date"] == ASOF.isoformat()
    assert result["bollinger_20bar"]["z_score"] == pytest.approx(19**0.5)
    assert result["bollinger_20bar"]["middle_band"] == 101
    assert result["rsi_14bar"]["value"] == 100
    assert (
        result["moving_averages"]["sma_20bar"]["window_start_date"]
        == (ASOF - timedelta(days=20)).isoformat()
    )


@pytest.mark.parametrize("calendar_gap", [False, True])
def test_query_and_cli_include_the_same_common_calendar_momentum(tmp_path, capsys, calendar_gap):
    path = tmp_path / "market.sqlite"
    insert_daily_bars_from_closes(path, "1234", list(range(100, 500)), end_date=ASOF)
    calendar = {
        ASOF - timedelta(days=n): (ASOF - timedelta(days=n)).weekday() < 5 for n in range(400)
    }
    if calendar_gap:
        del calendar[ASOF - timedelta(days=100)]
    with closing(open_connection(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_market_calendar(day,holiday_division) VALUES (?,?)",
            [(day.isoformat(), "1" if business else "0") for day, business in calendar.items()],
        )
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = technical_analysis(path, ticker="1234", as_of=ASOF)
    momentum = result["price_momentum"]
    if calendar_gap:
        assert momentum["status"] == "insufficient_calendar"
        assert momentum["value"] is None
    else:
        dates = sorted(day for day, business in calendar.items() if business)
        assert momentum["status"] == "available"
        assert momentum["start_date"] == dates[-253].isoformat()
        assert momentum["end_date"] == dates[-22].isoformat()
        assert momentum["value"] == pytest.approx(
            (499 - (ASOF - dates[-22]).days) / (499 - (ASOF - dates[-253]).days) - 1
        )
        shared = generate_technical_analysis(
            observed(list(range(100, 500))),
            ticker="1234",
            as_of=ASOF,
            trading_dates=dates,
            price_path=result["price_path"],
        )
        assert result == shared
    assert main(["show", "--ticker", "1234", "--asof", str(ASOF), "--db", str(path)]) == 0
    assert json.loads(capsys.readouterr().out) == result
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_calendar_missing_from_written_store_is_not_silently_unavailable(tmp_path):
    path = tmp_path / "market.sqlite"
    insert_daily_bars_from_closes(path, "1234", [100] * 30, end_date=ASOF)
    with closing(open_connection(path)) as connection, connection:
        connection.execute("DROP TABLE jquants_market_calendar")
    with pytest.raises(SQLiteSchemaError, match="jquants_market_calendar"):
        technical_analysis(path, ticker="1234", as_of=ASOF)
