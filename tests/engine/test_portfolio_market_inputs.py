"""Portfolio calendar and scoped prices must come from one read snapshot."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import add_source_coverage

from baibai_engine.position import market_source as market_store
from baibai_engine.position.market_source import market_basis_is_confirmed
from baibai_engine.storage.observations.read import connect_read_only
from baibai_engine.storage.observations.schema import open_connection

START = date(2026, 10, 14)
END = date(2026, 10, 15)


def _market_store(path: Path, *, calendar: bool = True, bars: bool = True) -> None:
    with closing(open_connection(path)) as connection, connection:
        if calendar:
            connection.executemany(
                "INSERT INTO jquants_market_calendar(day, holiday_division) VALUES (?, '1')",
                [(START.isoformat(),), (END.isoformat(),)],
            )
            add_source_coverage(
                connection,
                source="jquants_market_calendar",
                coverage_key="fixture-calendar",
                min_date=START.isoformat(),
                max_date=END.isoformat(),
                record_count=2,
            )
        if bars:
            connection.executemany(
                "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_factor) "
                "VALUES ('1301', ?, ?, 1.0)",
                [(START.isoformat(), 100.0), (END.isoformat(), 101.0)],
            )
            add_source_coverage(
                connection,
                source="jquants_daily_bars",
                coverage_key="fixture-prices",
                min_date=START.isoformat(),
                max_date=END.isoformat(),
                record_count=2,
            )


def test_portfolio_market_inputs_keep_one_snapshot_across_a_writer_commit(tmp_path, monkeypatch):
    database = tmp_path / "market.sqlite"
    _market_store(database)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    read_calendar = market_store._read_calendar

    def commit_after_calendar(connection, start, end):
        calendar = read_calendar(connection, start, end)
        # A real second connection commits between the two reads. WAL permits
        # this while the reader retains its original transaction snapshot.
        with closing(sqlite3.connect(database)) as writer, writer:
            writer.execute(
                "UPDATE jquants_market_calendar SET holiday_division = '0' WHERE day = ?",
                (END.isoformat(),),
            )
            writer.execute(
                "UPDATE jquants_daily_bars SET close = 999.0 WHERE ticker = '1301' AND traded_at = ?",
                (END.isoformat(),),
            )
        return calendar

    monkeypatch.setattr(market_store, "_read_calendar", commit_after_calendar)
    calendar, bars = market_store.read_portfolio_market_inputs(database, ("1301",), START, END)

    assert calendar is not None
    assert [(item.day, item.is_business_day) for item in calendar] == [(START, True), (END, True)]
    assert bars is not None
    assert [(item.traded_at, item.close) for item in bars] == [(START, 100.0), (END, 101.0)]
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute(
            "SELECT holiday_division FROM jquants_market_calendar WHERE day = ?",
            (END.isoformat(),),
        ).fetchone() == ("0",)
        assert connection.execute(
            "SELECT close FROM jquants_daily_bars WHERE ticker = '1301' AND traded_at = ?",
            (END.isoformat(),),
        ).fetchone() == (999.0,)


def test_portfolio_market_inputs_need_no_price_history_for_cash_only(tmp_path):
    database = tmp_path / "market.sqlite"
    _market_store(database, bars=False)

    calendar, bars = market_store.read_portfolio_market_inputs(database, (), START, END)

    assert calendar is not None
    assert [item.day for item in calendar if item.is_business_day] == [START, END]
    assert bars == []


@pytest.mark.parametrize("missing", ["store", "calendar", "bars", "ticker"])
def test_portfolio_market_inputs_preserve_unavailable_and_missing_ticker_distinctions(
    tmp_path, missing
):
    database = tmp_path / "market.sqlite"
    if missing != "store":
        _market_store(database, calendar=missing != "calendar", bars=missing != "bars")
    tickers = ("9999",) if missing == "ticker" else ("1301",)

    calendar, bars = market_store.read_portfolio_market_inputs(database, tickers, START, END)

    if missing in {"store", "calendar"}:
        assert calendar is None
        assert bars is None
    else:
        assert calendar is not None
        assert bars == ([] if missing == "ticker" else None)


def test_caller_keeps_the_same_snapshot_for_unit_checks_after_prices(tmp_path):
    database = tmp_path / "market.sqlite"
    _market_store(database)
    with closing(sqlite3.connect(database)) as setup:
        setup.execute("PRAGMA journal_mode = WAL")
    with closing(connect_read_only(database)) as reader:
        reader.execute("BEGIN")
        calendar, bars = market_store.read_portfolio_market_inputs(
            database, ("1301",), START, END, connection=reader
        )
        assert calendar is not None
        assert bars is not None
        with closing(sqlite3.connect(database)) as writer, writer:
            writer.execute(
                "UPDATE jquants_daily_bars SET adjustment_factor = 0.5 WHERE traded_at = ?",
                (END.isoformat(),),
            )
        assert market_basis_is_confirmed(
            basis="price",
            sqlite_path=database,
            ticker="1301",
            from_date=START,
            through_date=END,
            connection=reader,
        )
        assert reader.in_transaction
    assert not market_basis_is_confirmed(
        basis="price", sqlite_path=database, ticker="1301", from_date=START, through_date=END
    )
