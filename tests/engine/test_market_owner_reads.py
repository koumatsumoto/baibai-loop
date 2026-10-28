from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import pytest

from baibai_engine.machine_analysis.historical_evaluation.forward import latest_market_data_date
from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.sqlite_coverage.core import (
    _connect_readonly,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_reader import (
    read_next_earnings_dates,
)
from baibai_engine.machine_analysis.observations.disclosures.jpx_rebuild import (
    store_jpx_earnings_calendar_snapshot,
)
from baibai_engine.machine_analysis.observations.prices import calendar_reader
from baibai_engine.machine_analysis.observations.prices.adjusted_prices import read_price_bars
from baibai_engine.portfolio.holdings.market_observations import (
    market_basis_is_confirmed,
    next_order_session,
    read_unadjusted_close,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection


def _seed_bars(path: Path, rows: list[tuple[str, str, float | None]]) -> None:
    connection = open_connection(path)
    try:
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def _seed_earnings(path: Path, rows: list[tuple[str, str]]) -> None:
    store_jpx_earnings_calendar_snapshot(
        path,
        JPXEarningsCalendarSnapshot(
            entries=tuple(
                JPXEarningsCalendarEntry(ticker, date.fromisoformat(announcement_date))
                for ticker, announcement_date in rows
            ),
            source_urls=(),
            raw_record_count=len(rows),
            excluded_record_count=0,
            superseded_record_count=0,
        ),
        fetched_at_utc="2026-07-21T00:00:00+00:00",
    )


def test_next_earnings_dates_returns_current_dates_on_or_after_asof(
    tmp_path: Path,
) -> None:
    database = tmp_path / "market.sqlite"
    _seed_earnings(
        database,
        [
            ("2331", "2026-07-30"),
            ("4432", "2026-08-13"),
            ("5555", "2026-07-15"),  # before asof, ignored
            ("9999", "2026-07-31"),  # owner returns the complete current schedule
        ],
    )

    result = read_next_earnings_dates(database, date(2026, 7, 21))

    assert result == {
        "2331": date(2026, 7, 30),
        "4432": date(2026, 8, 13),
        "9999": date(2026, 7, 31),
    }


def test_next_earnings_dates_marks_missing_or_expired_schedule_unavailable(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.sqlite"
    assert read_next_earnings_dates(missing, date(2026, 7, 21)) is None

    database = tmp_path / "market.sqlite"
    _seed_earnings(database, [("2331", "2026-07-15")])
    assert read_next_earnings_dates(database, date(2026, 7, 21)) is None


def test_optional_earnings_dates_do_not_require_coverage_for_unrelated_market_reads(
    tmp_path: Path,
) -> None:
    database = tmp_path / "market.sqlite"
    _seed_bars(database, [("2331", "2026-07-21", 1000.0)])
    assert read_next_earnings_dates(database, date(2026, 7, 21)) is None
    quote = read_unadjusted_close(
        sqlite_path=database, ticker="2331", at=datetime.fromisoformat("2026-07-21T16:00:00+09:00")
    )
    assert quote.close_yen == 1000.0


def test_optional_earnings_view_does_not_publish_a_stale_schedule(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite"
    _seed_earnings(database, [("2331", "2026-08-13")])
    assert read_next_earnings_dates(database, date(2026, 8, 3)) is None


def test_earnings_view_propagates_corrupt_successful_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite"
    _seed_earnings(database, [("2331", "2026-07-30")])
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("UPDATE source_coverage SET record_count = 2")
    before = database.read_bytes()
    with pytest.raises(JPXProviderError, match="row count mismatch"):
        read_next_earnings_dates(database, date(2026, 7, 21))
    assert database.read_bytes() == before


def _seed_bars_with_factors(
    path: Path, rows: list[tuple[str, str, float | None, float | None]]
) -> None:
    connection = open_connection(path)
    try:
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_factor) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def _seed_calendar(path: Path, rows: list[tuple[str, int]]) -> None:
    connection = open_connection(path)
    try:
        connection.executemany(
            "INSERT INTO jquants_market_calendar(day, holiday_division) VALUES (?, ?)", rows
        )
        connection.commit()
    finally:
        connection.close()


def test_adjusted_price_reader_puts_both_ends_on_the_same_price_basis(tmp_path: Path) -> None:
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

    assert [
        bar.price
        for bar in read_price_bars(
            database, tickers=("2331",), start=date(2026, 7, 28), end=date(2026, 7, 29)
        )["2331"]
    ] == [500.0, 505.0]


def test_adjusted_price_reader_keeps_factor_on_a_close_null_day(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite"
    _seed_bars_with_factors(
        database,
        [
            ("2331", "2026-07-28", 1.0, 1.0),
            ("2331", "2026-07-29", None, 100.0),
            ("2331", "2026-07-30", 100.0, 1.0),
        ],
    )

    assert [
        bar.price
        for bar in read_price_bars(
            database, tickers=("2331",), start=date(2026, 7, 28), end=date(2026, 7, 30)
        )["2331"]
    ] == [100.0, 100.0]


@pytest.mark.parametrize("name", ["market #1.sqlite", "market?mode=rw.sqlite", "市場%20.sqlite"])
def test_readonly_market_readers_escape_uri_filename(tmp_path: Path, name: str) -> None:
    path = tmp_path / name
    day = date(2026, 10, 14)
    _seed_bars_with_factors(path, [("2331", day.isoformat(), 1000.0, 1.0)])
    _seed_calendar(path, [(day.isoformat(), 1), ("2026-10-15", 1)])
    now = datetime.fromisoformat("2026-10-14T16:00:00+09:00")

    quote = read_unadjusted_close(sqlite_path=path, ticker="2331", at=now)
    assert quote is not None
    assert quote.close_yen == 1000.0
    assert market_basis_is_confirmed(
        basis="shares", sqlite_path=path, ticker="2331", from_date=day, through_date=day
    )
    assert next_order_session(sqlite_path=path, now=now) == date(2026, 10, 15)
    assert latest_market_data_date(path) == day
    with closing(_connect_readonly(path)) as connection:
        assert connection.execute("SELECT MAX(traded_at) FROM jquants_daily_bars").fetchone() == (
            day.isoformat(),
        )
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM jquants_daily_bars")


def test_order_session_rejects_naive_instant_before_store_read(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite"
    naive = datetime(2026, 10, 14, 16)  # noqa: DTZ001 - deliberately invalid input
    with pytest.raises(ValueError, match="timezone"):
        next_order_session(sqlite_path=path, now=naive)
    assert not path.exists()


def test_adjusted_price_reader_keeps_ticker_scope_without_changing_the_store(
    tmp_path: Path,
) -> None:
    database = tmp_path / "market.sqlite"
    _seed_bars_with_factors(
        database,
        [
            ("2331", "2026-07-28", 100.0, 1.0),
            ("2331", "2026-07-29", 120.0, 1.0),
            ("2331", "2026-07-30", 90.0, 1.0),
            ("2331", "2026-07-31", 50.0, 0.5),
            ("4432", "2026-07-28", 1000.0, 1.0),
            ("4432", "2026-07-30", 1.0, 1.0),
        ],
    )
    before = database.read_bytes()
    result = read_price_bars(
        database, tickers=("2331", "2331"), start=date(2026, 7, 28), end=date(2026, 7, 31)
    )
    assert set(result) == {"2331"}
    assert [bar.price for bar in result["2331"]] == [50.0, 60.0, 45.0, 50.0]
    assert database.read_bytes() == before
    missing = tmp_path / "absent.sqlite"
    assert (
        read_price_bars(missing, tickers=(), start=date(2026, 7, 28), end=date(2026, 7, 31)) == {}
    )
    assert (
        read_price_bars(missing, tickers=("2331",), start=date(2026, 7, 28), end=date(2026, 7, 31))
        == {}
    )
    assert not missing.exists()


def test_calendar_query_failure_is_not_reported_as_missing_data(tmp_path, monkeypatch):
    database = tmp_path / "market.sqlite"
    _seed_calendar(database, [("2026-07-28", 1)])
    connection = sqlite3.connect(database)
    monkeypatch.setattr(jquants_reader, "connect_current", lambda _: connection)

    def failed_coverage_query(conn, *_args):
        return conn.execute("SELECT * FROM missing_coverage_table").fetchone()

    monkeypatch.setattr(jquants_reader, "range_covered", failed_coverage_query)
    with pytest.raises(sqlite3.OperationalError, match="missing_coverage_table"):
        calendar_reader.read_market_calendar(database, date(2026, 7, 28), date(2026, 7, 28))
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
