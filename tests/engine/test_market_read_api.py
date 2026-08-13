from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from baibai_engine.read_api.market import (
    close_change_since,
    latest_disclosure_dates_after,
    latest_unadjusted_closes,
    next_earnings_dates,
    previous_business_day,
    worst_close_drawdown,
)


def _seed_bars(path: Path, rows: list[tuple[str, str, float | None]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE jquants_daily_bars("
            "ticker TEXT NOT NULL, traded_at TEXT NOT NULL, close REAL, "
            "PRIMARY KEY (ticker, traded_at))"
        )
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def test_latest_unadjusted_closes_returns_newest_non_null_close_per_ticker(
    tmp_path: Path,
) -> None:
    database = tmp_path / "market.sqlite"
    _seed_bars(
        database,
        [
            ("2331", "2026-07-14", 1000.0),
            ("2331", "2026-07-17", 1055.0),
            ("2331", "2026-07-18", None),  # NULL close never wins over an earlier real close
            ("4432", "2026-07-16", 2500.5),
            ("9999", "2026-07-10", 300.0),  # not requested, stays out of the result
        ],
    )

    result = latest_unadjusted_closes(database, ["2331", "4432", "0000"])

    assert result == {
        "2331": (1055.0, date(2026, 7, 17)),
        "4432": (2500.5, date(2026, 7, 16)),
    }


def test_latest_unadjusted_closes_is_empty_without_file_or_tickers(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"
    assert latest_unadjusted_closes(missing, ["2331"]) == {}
    assert latest_unadjusted_closes(missing, []) == {}


def _seed_earnings(path: Path, rows: list[tuple[str, str]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE jquants_earnings_calendar("
            "announcement_date TEXT NOT NULL, ticker TEXT NOT NULL, "
            "PRIMARY KEY (announcement_date, ticker))"
        )
        connection.executemany(
            "INSERT INTO jquants_earnings_calendar(announcement_date, ticker) VALUES (?, ?)",
            [(announcement_date, ticker) for ticker, announcement_date in rows],
        )
        connection.commit()
    finally:
        connection.close()


def test_next_earnings_dates_returns_earliest_announcement_on_or_after_asof(
    tmp_path: Path,
) -> None:
    database = tmp_path / "market.sqlite"
    _seed_earnings(
        database,
        [
            ("2331", "2026-07-15"),  # before asof, ignored
            ("2331", "2026-07-30"),  # earliest on/after asof wins
            ("2331", "2026-10-30"),  # later, loses
            ("4432", "2026-08-13"),
            ("9999", "2026-07-31"),  # not requested, stays out of the result
        ],
    )

    result = next_earnings_dates(database, ["2331", "4432", "0000"], asof=date(2026, 7, 21))

    assert result == {
        "2331": date(2026, 7, 30),
        "4432": date(2026, 8, 13),
    }


def test_next_earnings_dates_is_empty_without_file_tickers_or_future_dates(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.sqlite"
    assert next_earnings_dates(missing, ["2331"], asof=date(2026, 7, 21)) == {}
    assert next_earnings_dates(missing, [], asof=date(2026, 7, 21)) == {}

    database = tmp_path / "market.sqlite"
    _seed_earnings(database, [("2331", "2026-07-15")])
    assert next_earnings_dates(database, ["2331"], asof=date(2026, 7, 21)) == {}


def _seed_bars_with_factors(
    path: Path, rows: list[tuple[str, str, float | None, float | None]]
) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE jquants_daily_bars("
            "ticker TEXT NOT NULL, traded_at TEXT NOT NULL, close REAL, "
            "adjustment_factor REAL, PRIMARY KEY (ticker, traded_at))"
        )
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_factor) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def _seed_calendar(path: Path, rows: list[tuple[str, int]]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE jquants_market_calendar("
            "day TEXT NOT NULL PRIMARY KEY, is_business_day INTEGER NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO jquants_market_calendar(day, is_business_day) VALUES (?, ?)", rows
        )
        connection.commit()
    finally:
        connection.close()


def test_close_change_since_measures_from_the_last_close_at_or_before_the_day(
    tmp_path: Path,
) -> None:
    database = tmp_path / "market.sqlite"
    _seed_bars_with_factors(
        database,
        [
            ("2331", "2026-07-27", 100.0, 1.0),
            ("2331", "2026-07-28", 110.0, 1.0),
            ("2331", "2026-07-29", 121.0, 1.0),
        ],
    )

    # The boundary is inclusive, so 07-28's own close is the earlier side.
    assert close_change_since(database, ["2331"], since=date(2026, 7, 28)) == {"2331": 10.0}
    assert close_change_since(database, ["2331"], since=date(2026, 7, 27)) == {"2331": 21.0}


def test_close_change_since_puts_both_ends_on_the_latest_share_basis(tmp_path: Path) -> None:
    # A 1:2 split halves the stored close. Reporting that as a 50% fall would send the
    # reader to review a holding that did not move.
    database = tmp_path / "market.sqlite"
    _seed_bars_with_factors(
        database,
        [
            ("2331", "2026-07-28", 1000.0, 1.0),
            ("2331", "2026-07-29", 505.0, 0.5),
        ],
    )

    assert close_change_since(database, ["2331"], since=date(2026, 7, 28)) == {"2331": 1.0}


def test_close_change_since_keeps_factor_on_a_close_null_day(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite"
    _seed_bars_with_factors(
        database,
        [
            ("2331", "2026-07-28", 1.0, 1.0),
            ("2331", "2026-07-29", None, 100.0),
            ("2331", "2026-07-30", 100.0, 1.0),
        ],
    )

    assert close_change_since(database, ["2331"], since=date(2026, 7, 28)) == {"2331": 0.0}


def test_worst_close_drawdown_keeps_factor_on_a_close_null_day(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite"
    _seed_bars_with_factors(
        database,
        [
            ("2331", "2026-07-28", 1.0, 1.0),
            ("2331", "2026-07-29", None, 100.0),
            ("2331", "2026-07-30", 50.0, 1.0),
        ],
    )

    assert worst_close_drawdown(
        database,
        ["2331"],
        start=date(2026, 7, 28),
        end=date(2026, 7, 30),
    ) == {"2331": -0.5}


def test_close_change_since_skips_a_ticker_with_no_close_by_the_day(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite"
    _seed_bars_with_factors(
        database,
        [
            ("2331", "2026-07-29", 100.0, 1.0),
            ("4432", "2026-07-28", 200.0, None),
            ("4432", "2026-07-29", None, None),
        ],
    )

    # 2331 has nothing to compare against; 4432's only later close is NULL.
    assert close_change_since(database, ["2331", "4432"], since=date(2026, 7, 28)) == {}


def test_close_change_since_is_empty_without_file_or_tickers(tmp_path: Path) -> None:
    assert close_change_since(tmp_path / "absent.sqlite", ["2331"], since=date(2026, 7, 28)) == {}


def test_latest_disclosure_dates_after_excludes_the_boundary_day(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(
            "CREATE TABLE jquants_fin_summaries("
            "ticker TEXT NOT NULL, disclosed_at TEXT NOT NULL, PRIMARY KEY (ticker, disclosed_at))"
        )
        connection.executemany(
            "INSERT INTO jquants_fin_summaries(ticker, disclosed_at) VALUES (?, ?)",
            [
                ("2331", "2026-07-28"),
                ("2331", "2026-07-29"),
                ("4432", "2026-07-28"),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    # Strictly after: a disclosure on the earlier as-of was already known then.
    result = latest_disclosure_dates_after(database, ["2331", "4432"], after=date(2026, 7, 28))

    assert result == {"2331": date(2026, 7, 29)}


def test_previous_business_day_skips_the_days_the_calendar_marks_closed(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite"
    _seed_calendar(
        database,
        [("2026-07-24", 1), ("2026-07-25", 0), ("2026-07-26", 0), ("2026-07-27", 1)],
    )

    assert previous_business_day(database, date(2026, 7, 27)) == date(2026, 7, 24)


def test_previous_business_day_is_unknown_when_the_calendar_stops_short(tmp_path: Path) -> None:
    # An uncovered day is not "closed": answering would draw the comparison against a
    # day nothing is known about.
    database = tmp_path / "market.sqlite"
    _seed_calendar(database, [("2026-07-27", 1)])

    assert previous_business_day(database, date(2026, 7, 27)) is None
