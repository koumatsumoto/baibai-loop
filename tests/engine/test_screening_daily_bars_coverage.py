from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from baibai_engine.market.store import (
    latest_daily_bar_date,
    latest_stored_daily_bar_date,
    read_adjustment_factor_bars,
    read_daily_bars,
)
from baibai_engine.storage.observations.schema import open_connection


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
    assert len(bars) == 91  # Synthetic calendar-day observations, including leap day.


def test_read_adjustment_factor_bars_returns_nonunit_price_events(tmp_path: Path) -> None:
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


def test_read_daily_bars_none_when_internal_gap_exceeds_tolerance(tmp_path: Path) -> None:
    # The constructed 20-day hole exceeds the reader's allowed date gap.
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


def test_daily_bars_short_gap_is_only_a_coverage_heuristic(tmp_path: Path) -> None:
    # The heuristic accepts this constructed six-day gap. It does not establish
    # that every missing date was a market holiday or that every ticker is complete.
    sqlite_path = _seed(
        tmp_path,
        [(date(2024, 1, 1), date(2024, 5, 2)), (date(2024, 5, 8), date(2024, 6, 30))],
    )
    bars = read_daily_bars(sqlite_path, date(2024, 1, 1), date(2024, 6, 30))
    assert bars is not None


def test_read_daily_bars_none_when_empty(tmp_path: Path) -> None:
    sqlite_path = _seed(tmp_path, [])
    assert read_daily_bars(sqlite_path, date(2024, 1, 1), date(2024, 3, 31)) is None


@pytest.mark.parametrize("scoped", [False, True])
def test_latest_bar_date_distinguishes_missing_from_invalid_storage(tmp_path, scoped):
    path = tmp_path / "market.sqlite"

    def read_latest():
        return (
            latest_daily_bar_date(path, date(2024, 1, 1), date(2024, 3, 31))
            if scoped
            else latest_stored_daily_bar_date(path)
        )

    assert read_latest() is None
    _seed(tmp_path, [])
    assert read_latest() is None
    connection = open_connection(path)
    try:
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
            ("1301", "2024-02-01-invalid", 1000),
        )
        connection.commit()
    finally:
        connection.close()
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Invalid isoformat string"):
        read_latest()
    assert path.read_bytes() == before
