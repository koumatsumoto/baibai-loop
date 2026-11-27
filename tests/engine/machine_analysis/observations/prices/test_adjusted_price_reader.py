"""銘柄別の調整係数と一括/scoped読取の一致を検証する。"""

from contextlib import closing
from datetime import date
from math import inf, nextafter

from tests.helpers.price_native import project_price_records

from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
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
