"""Technical L2 arithmetic, retained-prefix semantics, and read-only public outlets."""

import hashlib
import json
import socket
from contextlib import closing
from datetime import date, timedelta

import pytest
from tests.helpers.screening_sqlite import insert_daily_bars_from_closes

from baibai_engine.cli.technical_analysis import main
from baibai_engine.machine_analysis.generation.runners.technical_analysis import technical_analysis
from baibai_engine.machine_analysis.models.technical_analysis.calculations import (
    bollinger_20bar,
    rsi_14bar,
    simple_moving_average,
)
from baibai_engine.machine_analysis.models.technical_analysis.generation import (
    generate_technical_analysis,
)
from baibai_engine.machine_analysis.observations.prices.price_models import PriceBar
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError, open_connection

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


@pytest.mark.parametrize("count", [0, 74, 75, 76, 200])
def test_sma75_uses_its_observed_prefix_and_keeps_other_periods(count):
    bars = observed(list(range(100, 100 + count)))
    result = generate_technical_analysis(
        [PriceBar(ASOF + timedelta(days=1), 999999.0, None), *reversed(bars)],
        ticker="1234",
        as_of=ASOF,
        trading_dates=(),
    )
    averages = result["moving_averages"]
    assert set(averages) == {"sma_20bar", "sma_50bar", "sma_75bar", "sma_200bar"}
    assert result["calculation_revision"] == "sma20-50-75-200-bb20-wilder-rsi14-price-path-v4"
    for period in (20, 50, 75, 200):
        selected = bars[-period:]
        sma = averages[f"sma_{period}bar"]
        assert sma["window_bars"] == min(count, period)
        assert sma["window_start_date"] == (selected[0].traded_at.isoformat() if selected else None)
        assert sma["window_end_date"] == (ASOF.isoformat() if count else None)
        if count < period:
            assert sma["status"] == "insufficient_history"
            assert sma["value"] is sma["gap_ratio"] is None
        else:
            expected = (selected[0].price + selected[-1].price) / 2
            assert sma["status"] == "available"
            assert sma["value"] == expected
            assert sma["gap_ratio"] == pytest.approx(selected[-1].price / expected - 1)


@pytest.mark.parametrize("invalid", [0.0, -1.0, float("nan"), float("inf")])
@pytest.mark.parametrize("offset", [-76, -75])
def test_sma75_invalid_close_is_local_to_its_window(invalid, offset):
    prices = [100.0] * 76
    prices[offset] = invalid
    result = generate_technical_analysis(
        observed(prices), ticker="1234", as_of=ASOF, trading_dates=()
    )["moving_averages"]
    sma = result["sma_75bar"]
    if offset == -75:
        assert sma["status"] == "invalid_price"
        assert sma["value"] is sma["gap_ratio"] is None
    else:
        assert sma["status"] == "available"
        assert sma["value"] == 100
        assert sma["gap_ratio"] == 0
    assert result["sma_50bar"]["status"] == "available"
    assert result["sma_50bar"]["value"] == 100


def test_sma75_observed_bars_keep_null_day_gap_and_asof_adjustment_basis(tmp_path):
    path = tmp_path / "market.sqlite"
    insert_daily_bars_from_closes(path, "1234", [200] * 75 + [120], end_date=ASOF)
    with closing(open_connection(path)) as connection, connection:
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
    before = path.read_bytes()
    result = technical_analysis(path, ticker="1234", as_of=ASOF)
    assert result["bar_count"] == 75
    for period in (50, 75):
        sma = result["moving_averages"][f"sma_{period}bar"]
        assert sma["status"] == "available"
        assert sma["window_bars"] == period
        assert sma["window_start_date"] == (ASOF - timedelta(days=period)).isoformat()
        assert sma["window_end_date"] == ASOF.isoformat()
        expected = (100 * (period - 1) + 120) / period
        assert sma["value"] == pytest.approx(expected)
        assert sma["gap_ratio"] == pytest.approx(120 / expected - 1)
    assert path.read_bytes() == before


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


@pytest.mark.parametrize("calendar_state", ["complete", "gap", "short"])
def test_query_and_cli_include_the_same_common_calendar_momentum(tmp_path, capsys, calendar_state):
    path = tmp_path / "market.sqlite"
    insert_daily_bars_from_closes(path, "1234", list(range(100, 500)), end_date=ASOF)
    calendar = {
        ASOF - timedelta(days=n): (ASOF - timedelta(days=n)).weekday() < 5 for n in range(400)
    }
    if calendar_state == "gap":
        del calendar[ASOF - timedelta(days=100)]
    elif calendar_state == "short":
        calendar = {day: business for day, business in calendar.items() if (ASOF - day).days < 100}
    with closing(open_connection(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_market_calendar(day,holiday_division) VALUES (?,?)",
            [(day.isoformat(), "1" if business else "0") for day, business in calendar.items()],
        )
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = technical_analysis(path, ticker="1234", as_of=ASOF)
    momentum = result["price_momentum"]
    if calendar_state != "complete":
        assert momentum["status"] == "insufficient_calendar"
        assert momentum["value"] is None
        assert momentum["reference_date"] == (
            ASOF.isoformat() if calendar_state == "short" else None
        )
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


@pytest.mark.parametrize(
    "missing", [None, "turnover_value", "volume", "open", "close", "no_trade", "absent"]
)
def test_common_calendar_path_reads_retained_turnover_without_extra_source_or_future_factor(
    tmp_path,
    missing,
):
    path = tmp_path / "market.sqlite"
    dates = [ASOF - timedelta(days=i) for i in range(120)]
    traded = sorted(day for day in dates if day.weekday() < 5)
    closes = [100.0] * (len(traded) - 2) + [110.0, 100.0]
    with closing(open_connection(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_market_calendar(day,holiday_division) VALUES (?,?)",
            [(day.isoformat(), "1" if day.weekday() < 5 else "0") for day in dates],
        )
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,open,high,low,close,volume,turnover_value,adjustment_factor) "
            "VALUES('1234',?,?,?,?,?,?,?,1)",
            [
                (day.isoformat(), close, close, close, close, 10, 1000)
                for day, close in zip(traded, closes, strict=True)
            ],
        )
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close,adjustment_factor) VALUES('1234',?,99999,.01)",
            ((ASOF + timedelta(days=1)).isoformat(),),
        )
        if missing is not None:
            updates = {
                "turnover_value": "UPDATE jquants_daily_bars SET turnover_value=NULL WHERE traded_at=?",
                "volume": "UPDATE jquants_daily_bars SET volume=NULL WHERE traded_at=?",
                "open": "UPDATE jquants_daily_bars SET open=NULL WHERE traded_at=?",
                "close": "UPDATE jquants_daily_bars SET close=NULL WHERE traded_at=?",
                "no_trade": "UPDATE jquants_daily_bars SET open=NULL,high=NULL,low=NULL,close=NULL,volume=NULL,turnover_value=NULL WHERE traded_at=?",
                "absent": "DELETE FROM jquants_daily_bars WHERE traded_at=?",
            }
            connection.execute(updates[missing], (traded[-2].isoformat(),))
    before = path.read_bytes()
    result = technical_analysis(path, ticker="1234", as_of=ASOF)
    observation = result["price_path"]["observations"]["G60"]
    assert result["calculation_revision"].endswith("price-path-v4")
    assert result["price_path"]["calculation_revision"] == "common-calendar-observed-close-path-v3"
    assert observation["coverage"]["row_counts"]["priced"] == (61 if missing is None else 60)
    stats = observation["path_statistics"]
    if missing in {"close", "no_trade", "absent"}:
        assert stats["status"] == "incomplete_price_window"
        assert stats["log_path_efficiency"] is None
        assert stats["drawdown_episodes"] is None
    else:
        assert stats["status"] == "available"
        assert stats["up_days"] == 1
        assert stats["down_days"] == 1
        assert stats["log_path_efficiency"] == 0
        current = stats["drawdown_episodes"]["current"]
        assert stats["drawdown_episodes"]["latest_recovered"] is None
        assert current["peak_date"] == traded[-2].isoformat()
        assert current["trough_price"] == 100
        assert current["depth"] == pytest.approx(100 / 110 - 1)
        assert current["recovery_date"] is None
        assert current["peak_to_observation_trading_days"] == 1
        activity = stats["turnover_comparison"]
        if missing == "turnover_value":
            assert activity["up_down_mean_ratio"] is None
            assert activity["unavailable_reason"] == "missing_or_invalid_turnover"
            assert activity["up_valid_turnover_days"] == 0
            assert activity["down_mean_yen"] == 1000
        else:
            assert activity["up_down_mean_ratio"] == 1
    assert observation["comparison_end_price"] == 100
    assert result["moving_averages"]["sma_20bar"]["value"] == (
        100 if missing in {"close", "no_trade", "absent"} else 100.5
    )
    assert path.read_bytes() == before
