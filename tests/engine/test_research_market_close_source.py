"""`position/market_source.py`: the market read a thesis and a plan price against.

These were reached through the research CLI, which meant a schema-literal coupling
check and a WAL snapshot test were paying for a CLI invocation. The module has its
own boundary — a market store, a session, and a close — so it is tested at it.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.foundation.time import JST
from baibai_engine.position.market_source import (
    _EXPECTED_MARKET_SCHEMA_VERSION,
    quantity_basis_is_confirmed,
    read_unadjusted_close,
)


def test_market_close_source_expected_schema_version_tracks_market() -> None:
    # A market schema version bump changes SQLITE_SCHEMA_VERSION; this coupling
    # assertion turns that bump into a red CI check so the boundary-crossing schema
    # literals in market_close_source cannot drift silently.
    from baibai_engine.market.sqlite.schema import SQLITE_SCHEMA_VERSION

    assert _EXPECTED_MARKET_SCHEMA_VERSION == SQLITE_SCHEMA_VERSION


def test_market_close_source_degrades_on_schema_version_mismatch(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    conn = sqlite3.connect(sqlite_path)
    try:
        conn.execute(f"PRAGMA user_version = {_EXPECTED_MARKET_SCHEMA_VERSION + 999}")
        conn.commit()
    finally:
        conn.close()
    resolved = read_unadjusted_close(
        sqlite_path=sqlite_path, ticker="2331", at=datetime(2026, 7, 13, 9, tzinfo=JST)
    )
    assert resolved is None


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


def test_market_close_source_treats_missing_adjustment_factor_as_unresolved(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, None)])

    resolved = read_unadjusted_close(
        sqlite_path=sqlite_path, ticker="2331", at=datetime(2026, 7, 13, 9, tzinfo=JST)
    )

    assert resolved is not None
    assert resolved.corporate_action_unresolved is True


def test_market_close_source_reuses_one_stable_market_snapshot(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, 1.0)])
    with sqlite3.connect(sqlite_path) as writer:
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
        with sqlite3.connect(sqlite_path) as writer:
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
def test_holding_close_falls_back_when_revaluation_chain_is_incomplete(
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

    resolved = quantity_basis_is_confirmed(
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

    resolved = quantity_basis_is_confirmed(
        sqlite_path=sqlite_path,
        ticker="2331",
        from_date=date(2026, 7, 9),
        through_date=date(2026, 7, 10),
    )

    assert resolved is True


@pytest.mark.parametrize("close", [None, 0, -1, "bad", float("inf"), float("-inf")])
@pytest.mark.parametrize("factor", [None, 0.5, 1.0, "bad", float("inf")])
def test_holding_close_rejects_invalid_intermediate_values(tmp_path, close, factor):
    path = tmp_path / "market.sqlite"
    seed_daily_bars(path, [("2331", "2026-07-09", 990.0, 1.0), ("2331", "2026-07-10", 1000.0, 1.0)])
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE jquants_daily_bars SET close = ?, adjustment_factor = ? WHERE traded_at = ?",
            (close, factor, "2026-07-09"),
        )
    assert quantity_basis_is_confirmed(
        sqlite_path=path,
        ticker="2331",
        from_date=date(2026, 7, 9),
        through_date=date(2026, 7, 10),
    ) is (factor == 1.0)


@pytest.mark.parametrize("count", [1, 252])
def test_holding_close_uses_three_queries_and_keeps_connection(tmp_path, count):
    from datetime import timedelta

    path = tmp_path / "market.sqlite"
    start = date(2025, 1, 1)
    end = start + timedelta(days=count - 1)
    seed_daily_bars(
        path,
        [("2331", (start + timedelta(days=i)).isoformat(), 1000.0 + i, 1.0) for i in range(count)],
    )
    with sqlite3.connect(path) as conn:
        queries = []
        conn.set_trace_callback(queries.append)
        result = quantity_basis_is_confirmed(
            sqlite_path=path,
            ticker="2331",
            from_date=start,
            through_date=end,
            connection=conn,
        )
        assert result is True
        assert len(queries) == 3
        assert conn.execute("SELECT 1").fetchone() == (1,)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT traded_at, close, adjustment_factor FROM jquants_daily_bars WHERE ticker = ? AND traded_at BETWEEN ? AND ? ORDER BY traded_at",
            ("2331", start.isoformat(), end.isoformat()),
        ).fetchall()
        assert any("INDEX" in row[3] for row in plan)


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
    from baibai_engine.position.market_source import next_order_session

    path = tmp_path / "market.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE jquants_market_calendar(day TEXT,is_business_day INT)")
        connection.executemany(
            "INSERT INTO jquants_market_calendar VALUES (?,?)",
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
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM jquants_market_calendar WHERE day='2026-09-06'")
    assert (
        next_order_session(
            sqlite_path=path, now=datetime.fromisoformat("2026-09-05T10:00:00+09:00")
        )
        is None
    )
