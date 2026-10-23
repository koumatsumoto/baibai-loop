from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import insert_financial_record

from baibai_engine.market import store as market_store
from baibai_engine.market.jpx_models import JPXEarningsCalendarEntry, JPXEarningsCalendarSnapshot
from baibai_engine.position.market_source import (
    market_basis_is_confirmed,
    next_order_session,
    read_unadjusted_close,
)
from baibai_engine.read_api.market import (
    close_change_since,
    latest_disclosure_dates_after,
    latest_unadjusted_closes,
    next_earnings_dates,
    previous_business_day,
)
from baibai_engine.screening.calibration.forward import latest_market_data_date
from baibai_engine.screening.sqlite_coverage.core import _connect_readonly
from baibai_engine.storage.observations.ingest.jpx import store_jpx_earnings_calendar_snapshot
from baibai_engine.storage.observations.schema import open_connection


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


def test_next_earnings_dates_returns_requested_current_dates_on_or_after_asof(
    tmp_path: Path,
) -> None:
    database = tmp_path / "market.sqlite"
    _seed_earnings(
        database,
        [
            ("2331", "2026-07-30"),
            ("4432", "2026-08-13"),
            ("5555", "2026-07-15"),  # before asof, ignored
            ("9999", "2026-07-31"),  # not requested, stays out of the result
        ],
    )

    result = next_earnings_dates(database, ["2331", "4432", "5555", "0000"], asof=date(2026, 7, 21))

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


def test_optional_earnings_dates_do_not_require_coverage_for_unrelated_market_reads(
    tmp_path: Path,
) -> None:
    database = tmp_path / "market.sqlite"
    _seed_bars(database, [("2331", "2026-07-21", 1000.0)])
    assert next_earnings_dates(database, ["2331"], asof=date(2026, 7, 21)) == {}
    assert latest_unadjusted_closes(database, ["2331"]) == {"2331": (1000.0, date(2026, 7, 21))}


def test_current_schedule_with_no_requested_ticker_is_a_normal_empty_view(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite"
    _seed_earnings(database, [("4432", "2026-07-21")])
    assert next_earnings_dates(database, ["2331"], asof=date(2026, 7, 21)) == {}
    assert next_earnings_dates(database, ["4432"], asof=date(2026, 7, 21)) == {
        "4432": date(2026, 7, 21)
    }


def test_optional_earnings_view_does_not_publish_a_stale_schedule(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite"
    _seed_earnings(database, [("2331", "2026-08-13")])
    assert next_earnings_dates(database, ["2331"], asof=date(2026, 8, 3)) == {}


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
    connection = open_connection(database)
    try:
        for ticker, day in (
            ("2331", "2026-07-28"),
            ("2331", "2026-07-29"),
            ("4432", "2026-07-28"),
        ):
            insert_financial_record(
                connection,
                {"Code": f"{ticker}0", "DiscDate": day, "DiscNo": f"{ticker}-{day}"},
            )
        connection.commit()
    finally:
        connection.close()

    # The comparison excludes the earlier date and includes the later date.
    result = latest_disclosure_dates_after(
        database, ["2331", "4432"], after=date(2026, 7, 28), through_date=date(2026, 7, 29)
    )

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


def test_close_change_keeps_ticker_scope_without_changing_the_store(tmp_path: Path) -> None:
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
    assert close_change_since(database, ["2331", "2331"], since=date(2026, 7, 28)) == {"2331": 0.0}
    assert database.read_bytes() == before
    missing = tmp_path / "absent.sqlite"
    assert close_change_since(missing, [], since=date(2026, 7, 28)) == {}
    assert not missing.exists()


def test_calendar_query_failure_is_not_reported_as_missing_data(tmp_path, monkeypatch):
    database = tmp_path / "market.sqlite"
    _seed_calendar(database, [("2026-07-28", 1)])
    connection = sqlite3.connect(database)
    monkeypatch.setattr(market_store, "connect_current", lambda _: connection)

    def failed_coverage_query(conn, *_args):
        return conn.execute("SELECT * FROM missing_coverage_table").fetchone()

    monkeypatch.setattr(market_store, "range_covered", failed_coverage_query)
    with pytest.raises(sqlite3.OperationalError, match="missing_coverage_table"):
        market_store.read_market_calendar(database, date(2026, 7, 28), date(2026, 7, 28))
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
