"""Portfolio basis checks consume the calendar and prices on the caller's snapshot."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import add_source_coverage

import baibai_engine.portfolio.holdings.market_observations as market_store
from baibai_engine.machine_analysis.observations.jquants_decode import JQuantsProviderError
from baibai_engine.machine_analysis.observations.prices.calendar_reader import read_market_calendar
from baibai_engine.machine_analysis.observations.prices.price_reader import read_ticker_bars
from baibai_engine.portfolio.holdings.market_observations import market_basis_is_confirmed
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

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


def test_portfolio_only_decodes_requested_traded_closes(tmp_path):
    database = tmp_path / "market.sqlite"
    _market_store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES (?,?,?)",
            [("2345", END.isoformat(), None), ("3456", END.isoformat(), "broken")],
        )

    with closing(connect_read_only(database)) as connection:
        connection.execute("BEGIN")
        bars = read_ticker_bars(connection, {START: {"1301"}, END: {"1301", "2345"}})
        assert bars is not None
        assert [(bar.ticker, bar.close) for bar in bars] == [("1301", 100.0), ("1301", 101.0)]
        with pytest.raises(JQuantsProviderError, match=r"corrupt SQLite row.*3456"):
            read_ticker_bars(connection, {END: {"3456"}})


def test_scoped_marks_are_read_in_bounded_batches(tmp_path):
    database = tmp_path / "market.sqlite"
    _market_store(database)
    with closing(connect_read_only(database)) as connection:
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
        trace = []
        connection.set_trace_callback(trace.append)
        # 1,002 distinct pairs, but only two exist in the fixture.
        tickers = {str(ticker) for ticker in range(1301, 1802)}
        bars = read_ticker_bars(connection, {START: tickers, END: tickers})
        assert bars is not None
        assert [(bar.ticker, bar.traded_at) for bar in bars] == [("1301", START), ("1301", END)]
        bar_reads = [sql for sql in trace if "JOIN jquants_daily_bars" in sql]
        assert 1 < len(bar_reads) < 10


@pytest.mark.parametrize("missing", ["store", "calendar", "bars", "ticker"])
def test_market_input_owners_preserve_unavailable_and_missing_ticker_distinctions(
    tmp_path, missing
):
    database = tmp_path / "market.sqlite"
    if missing != "store":
        _market_store(database, calendar=missing != "calendar", bars=missing != "bars")
    calendar = read_market_calendar(database, START, END)
    if missing in {"store", "calendar"}:
        assert calendar is None
    else:
        assert calendar is not None
        with closing(connect_read_only(database)) as connection:
            connection.execute("BEGIN")
            ticker = "9999" if missing == "ticker" else "1301"
            assert read_ticker_bars(connection, {START: {ticker}, END: {ticker}}) == (
                [] if missing == "ticker" else None
            )


def test_caller_keeps_the_same_snapshot_for_unit_checks_after_prices(tmp_path):
    database = tmp_path / "market.sqlite"
    _market_store(database)
    with closing(sqlite3.connect(database)) as setup:
        setup.execute("PRAGMA journal_mode = WAL")
    with closing(connect_read_only(database)) as reader:
        reader.execute("BEGIN")
        calendar = read_market_calendar(database, START, END, connection=reader)
        bars = read_ticker_bars(reader, {START: {"1301"}, END: {"1301"}})
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


def _three_day_basis_store(path: Path) -> date:
    _market_store(path)
    first = START - timedelta(days=1)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "INSERT INTO jquants_market_calendar(day, holiday_division) VALUES (?, '1')",
            (first.isoformat(),),
        )
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_factor) "
            "VALUES ('1301', ?, 99.0, 1.0)",
            (first.isoformat(),),
        )
        add_source_coverage(
            connection,
            source="jquants_market_calendar",
            coverage_key="fixture-calendar",
            min_date=first.isoformat(),
            max_date=END.isoformat(),
            record_count=3,
        )
    return first


@pytest.mark.parametrize("basis", ["price", "shares"])
@pytest.mark.parametrize(
    "missing", [None, "entire-session", "ticker-session", "calendar-row", "calendar-coverage"]
)
def test_basis_requires_calendar_and_ticker_evidence_for_every_exchange_session(
    tmp_path, basis, missing
):
    database = tmp_path / "market.sqlite"
    first = _three_day_basis_store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        if missing in {"entire-session", "ticker-session"}:
            connection.execute(
                "DELETE FROM jquants_daily_bars WHERE traded_at = ?", (START.isoformat(),)
            )
            if missing == "ticker-session":
                connection.execute(
                    "INSERT INTO jquants_daily_bars(ticker, traded_at, adjustment_factor) "
                    "VALUES ('9999', ?, 1.0)",
                    (START.isoformat(),),
                )
        elif missing == "calendar-row":
            connection.execute(
                "DELETE FROM jquants_market_calendar WHERE day = ?", (START.isoformat(),)
            )
        elif missing == "calendar-coverage":
            connection.execute(
                "DELETE FROM source_coverage WHERE source = 'jquants_market_calendar'"
            )

    assert market_basis_is_confirmed(
        basis=basis,
        sqlite_path=database,
        ticker="1301",
        from_date=first,
        through_date=END,
    ) is (missing is None)


@pytest.mark.parametrize(
    ("factor", "kind", "price_confirmed", "shares_confirmed"),
    [
        (1.0, None, True, True),
        (0.8, "3", False, True),
        (0.5, "1", False, False),
        (0.8, None, False, False),
        (None, None, False, False),
    ],
)
def test_calendar_based_confirmation_preserves_price_and_share_action_semantics(
    tmp_path, factor, kind, price_confirmed, shares_confirmed
):
    database = tmp_path / "market.sqlite"
    first = _three_day_basis_store(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET adjustment_factor = ?, ex_right_type = ? "
            "WHERE ticker = '1301' AND traded_at = ?",
            (factor, kind, START.isoformat()),
        )
    for basis, expected in (("price", price_confirmed), ("shares", shares_confirmed)):
        assert (
            market_basis_is_confirmed(
                basis=basis,
                sqlite_path=database,
                ticker="1301",
                from_date=first,
                through_date=END,
            )
            is expected
        )


def test_basis_calendar_and_action_rows_share_one_read_snapshot(tmp_path, monkeypatch):
    database = tmp_path / "market.sqlite"
    first = _three_day_basis_store(database)
    with closing(sqlite3.connect(database)) as setup:
        assert setup.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    read_calendar = market_store.read_market_calendar

    def remove_calendar_before_read(path, start, end, *, connection):
        # Action rows are already materialized; commit before the calendar read
        # so removing BEGIN exposes mixed snapshots in the first assertion.
        with closing(sqlite3.connect(database)) as writer, writer:
            writer.execute(
                "DELETE FROM jquants_market_calendar WHERE day = ?", (START.isoformat(),)
            )
        return read_calendar(path, start, end, connection=connection)

    monkeypatch.setattr(market_store, "read_market_calendar", remove_calendar_before_read)
    assert market_basis_is_confirmed(
        basis="shares", sqlite_path=database, ticker="1301", from_date=first, through_date=END
    )
    monkeypatch.setattr(market_store, "read_market_calendar", read_calendar)
    assert not market_basis_is_confirmed(
        basis="shares", sqlite_path=database, ticker="1301", from_date=first, through_date=END
    )
