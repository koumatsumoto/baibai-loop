"""`research/market_close_source.py`: the market read a thesis and a plan price against.

These were reached through the research CLI, which meant a schema-literal coupling
check and a WAL snapshot test were paying for a CLI invocation. The module has its
own boundary — a market store, a session, and a close — so it is tested at it.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from tests.helpers.screening_sqlite import seed_daily_bars

from baibai_engine.research.market_close_source import (
    _EXPECTED_MARKET_SCHEMA_VERSION,
    read_holding_unadjusted_close_on_basis,
    read_prior_session_unadjusted_close,
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
    resolved = read_prior_session_unadjusted_close(
        sqlite_path=sqlite_path, ticker="2331", target_session=date(2026, 7, 13)
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

    resolved = read_prior_session_unadjusted_close(
        sqlite_path=sqlite_path, ticker="2331", target_session=date(2026, 7, 13)
    )

    assert resolved is None


def test_market_close_source_treats_missing_adjustment_factor_as_unresolved(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(sqlite_path, [("2331", "2026-07-10", 1000.0, None)])

    resolved = read_prior_session_unadjusted_close(
        sqlite_path=sqlite_path, ticker="2331", target_session=date(2026, 7, 13)
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
        first = read_prior_session_unadjusted_close(
            sqlite_path=sqlite_path,
            ticker="2331",
            target_session=date(2026, 7, 13),
            connection=reader,
        )
        with sqlite3.connect(sqlite_path) as writer:
            writer.execute("UPDATE jquants_daily_bars SET close = 1200 WHERE ticker = '2331'")
        second = read_prior_session_unadjusted_close(
            sqlite_path=sqlite_path,
            ticker="2331",
            target_session=date(2026, 7, 13),
            connection=reader,
        )
    finally:
        reader.close()

    current = read_prior_session_unadjusted_close(
        sqlite_path=sqlite_path, ticker="2331", target_session=date(2026, 7, 13)
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

    resolved = read_holding_unadjusted_close_on_basis(
        sqlite_path=sqlite_path,
        ticker="2331",
        ledger_price_observed_on=date(2026, 7, 8),
        basis_as_of=date(2026, 7, 10),
    )

    assert resolved is None


def test_holding_close_uses_exact_basis_after_complete_raw_chain(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    seed_daily_bars(
        sqlite_path,
        [
            ("2331", "2026-07-09", 990.0, 1.0),
            ("2331", "2026-07-10", 1000.0, 1.0),
        ],
    )

    resolved = read_holding_unadjusted_close_on_basis(
        sqlite_path=sqlite_path,
        ticker="2331",
        ledger_price_observed_on=date(2026, 7, 9),
        basis_as_of=date(2026, 7, 10),
    )

    assert resolved is not None
    assert resolved.price_as_of == date(2026, 7, 10)
    assert resolved.close_yen == 1000.0


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
    assert (
        read_holding_unadjusted_close_on_basis(
            sqlite_path=path,
            ticker="2331",
            ledger_price_observed_on=date(2026, 7, 9),
            basis_as_of=date(2026, 7, 10),
        )
        is None
    )


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
        result = read_holding_unadjusted_close_on_basis(
            sqlite_path=path,
            ticker="2331",
            ledger_price_observed_on=start,
            basis_as_of=end,
            connection=conn,
        )
        assert result is not None
        assert result.close_yen == 999.0 + count
        assert len(queries) == 3
        assert conn.execute("SELECT 1").fetchone() == (1,)
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT traded_at, close, adjustment_factor FROM jquants_daily_bars WHERE ticker = ? AND traded_at BETWEEN ? AND ? ORDER BY traded_at",
            ("2331", start.isoformat(), end.isoformat()),
        ).fetchall()
        assert any("INDEX" in row[3] for row in plan)
