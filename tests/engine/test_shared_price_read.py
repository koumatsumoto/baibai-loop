from contextlib import closing
from datetime import date

from baibai_engine.market.adjusted_prices import read_price_bars
from baibai_engine.storage.observations.schema import open_connection


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
