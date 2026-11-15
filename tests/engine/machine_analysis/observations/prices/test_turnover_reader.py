"""Independent yen observations retain their finite source interval and missingness."""

from contextlib import closing
from datetime import date

from baibai_engine.machine_analysis.observations.prices.price_observation_reader import (
    read_turnover_values,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def test_turnover_reader_does_not_require_a_close_or_scale_with_price_actions(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as connection:
        connection.executemany(
            "INSERT INTO jquants_daily_bars"
            "(ticker,traded_at,close,turnover_value,adjustment_factor) VALUES (?,?,?,?,?)",
            [
                ("1111", "2026-01-04", None, 1e12, 1.0),
                ("1111", "2026-01-05", None, 100.0, 0.5),
                ("1111", "2026-01-06", None, 0.0, 1.0),
                ("1111", "2026-01-07", 100.0, None, 1.0),
                ("1111", "2026-01-08", 100.0, -1.0, 1.0),
                ("1111", "2026-01-09", None, 1e12, 1.0),
                ("2222", "2026-01-05", None, 200.0, 1.0),
            ],
        )
        connection.commit()
        assert read_turnover_values(
            connection,
            start=date(2026, 1, 5),
            end=date(2026, 1, 8),
            tickers=("1111", "3333"),
        ) == {
            "1111": {
                date(2026, 1, 5): 100.0,
                date(2026, 1, 6): 0.0,
                date(2026, 1, 7): None,
                date(2026, 1, 8): -1.0,
            }
        }
        assert read_turnover_values(connection, start=date.min, end=date.max, tickers=()) == {}
