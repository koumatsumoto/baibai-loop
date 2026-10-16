"""注文・保有評価が使う市場終値と株式数basisの読取境界を検証する。

市場storeのschema、営業日、安定したSQLite snapshotを直接検証する。
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.foundation.time import JST
from baibai_engine.position.market_source import (
    market_basis_is_confirmed,
    next_order_session,
    read_unadjusted_close,
)
from baibai_engine.storage.observations.schema import (
    SQLITE_SCHEMA_VERSION,
    SQLiteSchemaError,
    open_connection,
)


def test_market_close_source_reports_schema_version_mismatch(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION + 999}")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(SQLiteSchemaError):
        read_unadjusted_close(
            sqlite_path=sqlite_path, ticker="2331", at=datetime(2026, 7, 13, 9, tzinfo=JST)
        )


def test_market_close_source_never_uses_older_ticker_bar_when_market_wide_date_is_missing(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(
        sqlite_path,
        [
            ("2331", "2026-07-09", 990.0, 1.0),
            ("9999", "2026-07-10", 500.0, 1.0),
        ],
    )

    resolved = read_unadjusted_close(
        sqlite_path=sqlite_path, ticker="2331", at=datetime(2026, 7, 13, 9, tzinfo=JST)
    )

    assert resolved is None


@pytest.mark.parametrize("factor", [None, 0.0, 0.5, 1.0, 2.0])
def test_raw_close_is_available_independently_of_adjustment_metadata(tmp_path: Path, factor) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, factor)])

    resolved = read_unadjusted_close(
        sqlite_path=sqlite_path, ticker="2331", at=datetime(2026, 7, 13, 9, tzinfo=JST)
    )

    assert resolved is not None
    assert resolved.close_yen == 1000.0
    assert resolved.price_as_of == date(2026, 7, 10)


def test_market_close_source_reuses_one_stable_market_snapshot(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    with closing(sqlite3.connect(sqlite_path)) as writer, writer:
        writer.execute("PRAGMA journal_mode = WAL")

    reader = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    try:
        reader.execute("BEGIN")
        first = read_unadjusted_close(
            sqlite_path=sqlite_path,
            ticker="2331",
            at=datetime(2026, 7, 13, 9, tzinfo=JST),
            connection=reader,
        )
        with closing(sqlite3.connect(sqlite_path)) as writer, writer:
            writer.execute("UPDATE jquants_daily_bars SET close = 1200 WHERE ticker = '2331'")
        second = read_unadjusted_close(
            sqlite_path=sqlite_path,
            ticker="2331",
            at=datetime(2026, 7, 13, 9, tzinfo=JST),
            connection=reader,
        )
    finally:
        reader.close()

    current = read_unadjusted_close(
        sqlite_path=sqlite_path, ticker="2331", at=datetime(2026, 7, 13, 9, tzinfo=JST)
    )
    assert first is not None
    assert second is not None
    assert current is not None
    assert first.close_yen == second.close_yen == 1000.0
    assert current.close_yen == 1200.0


@pytest.mark.parametrize(
    ("intermediate_close", "intermediate_factor"),
    [(None, 1.0), (995.0, None), (995.0, 0.5)],
    ids=("missing-bar", "missing-factor", "non-unit-factor"),
)
def test_quantity_basis_rejects_an_incomplete_adjustment_chain(
    tmp_path: Path,
    intermediate_close: float | None,
    intermediate_factor: float | None,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    rows: list[tuple[str, str, float | None, float | None]] = [
        ("2331", "2026-07-08", 990.0, 1.0),
        ("2331", "2026-07-10", 1000.0, 1.0),
    ]
    if intermediate_close is None:
        rows.append(("9999", "2026-07-09", 500.0, 1.0))
    else:
        rows.append(("2331", "2026-07-09", intermediate_close, intermediate_factor))
    seed_daily_bars(sqlite_path, rows)

    resolved = market_basis_is_confirmed(
        basis="shares",
        sqlite_path=sqlite_path,
        ticker="2331",
        from_date=date(2026, 7, 8),
        through_date=date(2026, 7, 10),
    )

    assert resolved is False


def test_holding_close_uses_exact_basis_after_complete_raw_chain(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(
        sqlite_path,
        [
            ("2331", "2026-07-09", 990.0, 1.0),
            ("2331", "2026-07-10", 1000.0, 1.0),
        ],
    )

    resolved = market_basis_is_confirmed(
        basis="shares",
        sqlite_path=sqlite_path,
        ticker="2331",
        from_date=date(2026, 7, 9),
        through_date=date(2026, 7, 10),
    )

    assert resolved is True


@pytest.mark.parametrize("close", [None, 0, -1, "bad", float("inf"), float("-inf")])
@pytest.mark.parametrize("factor", [None, 0.5, 1.0, "bad", float("inf")])
def test_quantity_basis_depends_on_adjustment_factor_not_price(tmp_path, close, factor):
    path = tmp_path / "market.sqlite"
    seed_daily_bars(path, [("2331", "2026-07-09", 990.0, 1.0), ("2331", "2026-07-10", 1000.0, 1.0)])
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE jquants_daily_bars SET close = ?, adjustment_factor = ? WHERE traded_at = ?",
            (close, factor, "2026-07-09"),
        )
    assert market_basis_is_confirmed(
        basis="shares",
        sqlite_path=path,
        ticker="2331",
        from_date=date(2026, 7, 9),
        through_date=date(2026, 7, 10),
    ) is (factor == 1.0)


@pytest.mark.parametrize("count", [1, 252])
def test_quantity_basis_keeps_the_callers_connection(tmp_path, count):
    from datetime import timedelta

    path = tmp_path / "market.sqlite"
    start = date(2025, 1, 1)
    end = start + timedelta(days=count - 1)
    seed_daily_bars(
        path,
        [("2331", (start + timedelta(days=i)).isoformat(), 1000.0 + i, 1.0) for i in range(count)],
    )
    with closing(sqlite3.connect(path)) as conn, conn:
        result = market_basis_is_confirmed(
            basis="shares",
            sqlite_path=path,
            ticker="2331",
            from_date=start,
            through_date=end,
            connection=conn,
        )
        assert result is True
        assert conn.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize(
    ("instant", "session"),
    [
        ("2026-09-07T08:00:00+09:00", "2026-09-07"),
        ("2026-09-07T12:00:00+09:00", "2026-09-07"),
        ("2026-09-07T15:29:59+09:00", "2026-09-07"),
        ("2026-09-07T15:30:00+09:00", "2026-09-08"),
        ("2026-09-07T09:00:00+00:00", "2026-09-08"),
        ("2026-09-05T10:00:00+09:00", "2026-09-07"),
        ("2026-09-06T18:00:00+09:00", "2026-09-07"),
    ],
)
def test_order_session_uses_calendar_and_unexpired_market_close(tmp_path, instant, session):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO jquants_market_calendar(day, holiday_division) VALUES (?,?)",
            [
                ("2026-09-05", 0),
                ("2026-09-06", 0),
                ("2026-09-07", 1),
                ("2026-09-08", 1),
            ],
        )
    assert next_order_session(
        sqlite_path=path, now=datetime.fromisoformat(instant)
    ) == date.fromisoformat(session)
    # An uncovered holiday must not be skipped to a later known trading day.
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DELETE FROM jquants_market_calendar WHERE day='2026-09-06'")
    assert (
        next_order_session(
            sqlite_path=path, now=datetime.fromisoformat("2026-09-05T10:00:00+09:00")
        )
        is None
    )


@pytest.mark.parametrize("operation", ["close", "quantity", "session"])
@pytest.mark.parametrize("state", ["absent", "corrupt", "obsolete", "missing_table"])
def test_market_read_failure_is_not_normal_missing_data(tmp_path, operation, state):
    path = tmp_path / "market.sqlite"
    if state == "corrupt":
        path.write_bytes(b"not SQLite")
    elif state != "absent":
        with closing(open_connection(path)) as connection, connection:
            if state == "obsolete":
                connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION - 1}")
            else:
                connection.execute("DROP TABLE jquants_daily_bars")

    def read():
        if operation == "close":
            return read_unadjusted_close(
                sqlite_path=path, ticker="2331", at=datetime(2026, 7, 13, 9, tzinfo=JST)
            )
        if operation == "quantity":
            return market_basis_is_confirmed(
                basis="shares",
                sqlite_path=path, ticker="2331",
                from_date=date(2026, 7, 9), through_date=date(2026, 7, 10),
            )
        return next_order_session(
            sqlite_path=path, now=datetime(2026, 7, 13, 9, tzinfo=JST)
        )

    if state == "absent":
        assert read() is (False if operation == "quantity" else None)
        assert not path.exists()
    else:
        before = path.read_bytes()
        with pytest.raises((sqlite3.DatabaseError, SQLiteSchemaError)):
            read()
        assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("kind", "factor", "shares_unchanged", "price_unchanged"),
    [
        (None, 1.0, True, True),
        ("3", 0.8, True, False),
        ("1", 0.5, False, False),
        ("2", 2.0, False, False),
        (None, 0.8, False, False),
        ("unknown", 0.8, False, False),
        ("3", None, False, False),
    ],
)
def test_position_share_units_and_price_comparison_have_distinct_basis(
    tmp_path, kind, factor, shares_unchanged, price_unchanged
):
    path = tmp_path / "market.sqlite"
    seed_daily_bars(path, [
        ("2331", "2026-07-09", 1000.0, 1.0),
        ("2331", "2026-07-10", 800.0, factor),
    ])
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "UPDATE jquants_daily_bars SET ex_right_type = ? WHERE traded_at = ?",
            (kind, "2026-07-10"),
        )
    for basis, expected in (("shares", shares_unchanged), ("price", price_unchanged)):
        assert market_basis_is_confirmed(
            sqlite_path=path, ticker="2331", basis=basis,
            from_date=date(2026, 7, 9), through_date=date(2026, 7, 10),
        ) is expected
