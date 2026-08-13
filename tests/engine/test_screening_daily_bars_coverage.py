from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.market.store import read_adjustment_factor_bars, read_daily_bars
from baibai_engine.screening.sqlite_cache import open_connection


def _insert_bars(conn: sqlite3.Connection, ranges: list[tuple[date, date]]) -> None:
    for start, end in ranges:
        current = start
        while current <= end:
            conn.execute(
                "INSERT INTO jquants_daily_bars(ticker, traded_at, close, turnover_value) "
                "VALUES (?, ?, ?, ?)",
                ("1301", current.isoformat(), 1000.0, 1_000_000.0),
            )
            current += timedelta(days=1)


def _seed(tmp_path: Path, ranges: list[tuple[date, date]]) -> Path:
    sqlite_path = tmp_path / "market.sqlite"
    conn = open_connection(sqlite_path)
    _insert_bars(conn, ranges)
    conn.commit()
    conn.close()
    return sqlite_path


def test_read_daily_bars_returns_rows_when_window_is_dense(tmp_path: Path) -> None:
    sqlite_path = _seed(tmp_path, [(date(2024, 1, 1), date(2024, 3, 31))])
    bars = read_daily_bars(sqlite_path, date(2024, 1, 1), date(2024, 3, 31))
    assert bars is not None
    assert len(bars) > 80


def test_read_adjustment_factor_bars_returns_only_split_events(tmp_path: Path) -> None:
    sqlite_path = _seed(tmp_path, [(date(2024, 1, 1), date(2024, 3, 31))])
    conn = open_connection(sqlite_path)
    conn.execute(
        "UPDATE jquants_daily_bars SET adjustment_factor = 0.5 WHERE traded_at = ?",
        ("2024-02-01",),
    )
    conn.commit()
    conn.close()

    bars = read_adjustment_factor_bars(sqlite_path, date(2024, 1, 1), date(2024, 3, 31))

    assert bars is not None
    assert [(bar.traded_at, bar.adjustment_factor) for bar in bars] == [(date(2024, 2, 1), 0.5)]


def test_read_adjustment_factor_bars_keeps_event_without_close(tmp_path: Path) -> None:
    """取引停止日の価格欠損はcorporate-action eventを消さない。"""

    sqlite_path = _seed(tmp_path, [(date(2024, 1, 1), date(2024, 3, 31))])
    conn = open_connection(sqlite_path)
    conn.execute(
        "UPDATE jquants_daily_bars SET close = NULL, adjustment_factor = 100.0 WHERE traded_at = ?",
        ("2024-02-01",),
    )
    conn.commit()
    conn.close()

    events = read_adjustment_factor_bars(sqlite_path, date(2024, 1, 1), date(2024, 3, 31))

    assert events is not None
    assert [(event.traded_at, event.adjustment_factor) for event in events] == [
        (date(2024, 2, 1), 100.0)
    ]


def test_read_daily_bars_none_when_internal_gap_exceeds_holiday(tmp_path: Path) -> None:
    # 20-day hole in the middle is larger than any market holiday run.
    sqlite_path = _seed(
        tmp_path,
        [(date(2024, 1, 1), date(2024, 1, 20)), (date(2024, 2, 10), date(2024, 3, 31))],
    )
    assert read_daily_bars(sqlite_path, date(2024, 1, 1), date(2024, 3, 31)) is None


def test_read_daily_bars_none_when_history_missing_at_start(tmp_path: Path) -> None:
    # Data only covers the back half; the requested window starts 40 days early.
    sqlite_path = _seed(tmp_path, [(date(2024, 2, 10), date(2024, 3, 31))])
    assert read_daily_bars(sqlite_path, date(2024, 1, 1), date(2024, 3, 31)) is None


def test_read_daily_bars_none_when_history_missing_at_end(tmp_path: Path) -> None:
    sqlite_path = _seed(tmp_path, [(date(2024, 1, 1), date(2024, 2, 10))])
    assert read_daily_bars(sqlite_path, date(2024, 1, 1), date(2024, 3, 31)) is None


def test_read_daily_bars_covered_with_holiday_length_gap(tmp_path: Path) -> None:
    # A Golden Week style closure (2024-05-02 -> 2024-05-08, a 6-day gap) must
    # still count as covered.
    sqlite_path = _seed(
        tmp_path,
        [(date(2024, 1, 1), date(2024, 5, 2)), (date(2024, 5, 8), date(2024, 6, 30))],
    )
    bars = read_daily_bars(sqlite_path, date(2024, 1, 1), date(2024, 6, 30))
    assert bars is not None


def test_read_daily_bars_none_when_empty(tmp_path: Path) -> None:
    sqlite_path = _seed(tmp_path, [])
    assert read_daily_bars(sqlite_path, date(2024, 1, 1), date(2024, 3, 31)) is None
