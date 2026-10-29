"""Portfolio calendar and scoped prices must come from one read snapshot."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import add_source_coverage

import baibai_engine.portfolio.holdings.market_observations as market_store
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
    read_calendar = market_store._read_calendar

    def remove_action_after_calendar(connection, start, end):
        calendar = read_calendar(connection, start, end)
        with closing(sqlite3.connect(database)) as writer, writer:
            writer.execute(
                "DELETE FROM jquants_daily_bars WHERE traded_at = ?", (START.isoformat(),)
            )
        return calendar

    monkeypatch.setattr(market_store, "_read_calendar", remove_action_after_calendar)
    assert market_basis_is_confirmed(
        basis="shares", sqlite_path=database, ticker="1301", from_date=first, through_date=END
    )
    monkeypatch.setattr(market_store, "_read_calendar", read_calendar)
    assert not market_basis_is_confirmed(
        basis="shares", sqlite_path=database, ticker="1301", from_date=first, through_date=END
    )
