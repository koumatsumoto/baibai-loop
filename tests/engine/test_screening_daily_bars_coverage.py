from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from baibai_engine.market.price_reader import (
    daily_bars_covered,
    latest_daily_bar_date,
    latest_stored_daily_bar_date,
    read_adjustment_factor_bars,
    read_daily_bars,
    read_daily_bars_for_tickers,
)
from baibai_engine.storage.observations.coverage import record_range_source_coverage
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
    for start, end in ranges:
        record_range_source_coverage(
            conn,
            source="jquants_daily_bars",
            operation="get_daily_quotes",
            table="jquants_daily_bars",
            date_column="traded_at",
            requested_start=start,
            requested_end=end,
            status="ok",
            error=None,
        )
    conn.commit()
    conn.close()
    return sqlite_path


def test_read_daily_bars_returns_rows_when_window_is_dense(tmp_path: Path) -> None:
    sqlite_path = _seed(tmp_path, [(date(2024, 1, 1), date(2024, 3, 31))])
    bars = read_daily_bars(sqlite_path, date(2024, 1, 1), date(2024, 3, 31))
    assert bars is not None
    assert len(bars) == 91  # Synthetic calendar-day observations, including leap day.


def test_reversed_daily_bar_range_is_never_covered(tmp_path: Path) -> None:
    sqlite_path = _seed(tmp_path, [(date(2024, 1, 1), date(2024, 3, 31))])
    start, end = date(2024, 3, 31), date(2024, 1, 1)
    assert not daily_bars_covered(sqlite_path, start, end)
    assert read_daily_bars(sqlite_path, start, end) is None
    assert read_adjustment_factor_bars(sqlite_path, start, end) is None
    assert read_daily_bars_for_tickers(sqlite_path, ("1301",), start, end) is None


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


def test_read_daily_bars_none_when_internal_range_was_not_fetched(tmp_path: Path) -> None:
    sqlite_path = _seed(
        tmp_path,
        [(date(2024, 1, 1), date(2024, 1, 20)), (date(2024, 2, 10), date(2024, 3, 31))],
    )
    assert read_daily_bars(sqlite_path, date(2024, 1, 1), date(2024, 3, 31)) is None


def test_read_daily_bars_none_when_history_missing_at_start(tmp_path: Path) -> None:
    # Acquisition only covers the back half of the requested window.
    sqlite_path = _seed(tmp_path, [(date(2024, 2, 10), date(2024, 3, 31))])
    assert read_daily_bars(sqlite_path, date(2024, 1, 1), date(2024, 3, 31)) is None


def test_read_daily_bars_none_when_history_missing_at_end(tmp_path: Path) -> None:
    sqlite_path = _seed(tmp_path, [(date(2024, 1, 1), date(2024, 2, 10))])
    assert read_daily_bars(sqlite_path, date(2024, 1, 1), date(2024, 3, 31)) is None


def test_daily_bars_short_unfetched_gap_requires_explicit_coverage(tmp_path: Path) -> None:
    sqlite_path = _seed(
        tmp_path,
        [(date(2024, 1, 1), date(2024, 5, 2)), (date(2024, 5, 8), date(2024, 6, 30))],
    )
    start, end = date(2024, 1, 1), date(2024, 6, 30)
    assert read_daily_bars(sqlite_path, start, end) is None
    conn = open_connection(sqlite_path)
    try:
        record_range_source_coverage(
            conn,
            source="jquants_daily_bars",
            operation="get_daily_quotes",
            table="jquants_daily_bars",
            date_column="traded_at",
            requested_start=date(2024, 5, 3),
            requested_end=date(2024, 5, 7),
            status="ok",
            error=None,
        )
        conn.commit()
    finally:
        conn.close()
    assert daily_bars_covered(sqlite_path, start, end)
    assert read_daily_bars(sqlite_path, start, end) is not None


def test_successful_empty_daily_bars_are_distinct_from_unfetched(tmp_path: Path) -> None:
    sqlite_path = _seed(tmp_path, [])
    target = date(2024, 1, 1)
    assert read_daily_bars(sqlite_path, target, target) is None
    conn = open_connection(sqlite_path)
    try:
        record_range_source_coverage(
            conn,
            source="jquants_daily_bars",
            operation="get_daily_quotes",
            table="jquants_daily_bars",
            date_column="traded_at",
            requested_start=target,
            requested_end=target,
            status="ok",
            error=None,
        )
        conn.commit()
    finally:
        conn.close()
    assert daily_bars_covered(sqlite_path, target, target)
    assert read_daily_bars(sqlite_path, target, target) == []


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
