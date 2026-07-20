"""Forward-only migration mechanism and coverage invalidation for market.sqlite.

These exercise the real `open_connection` upgrade path (create at latest,
forward-migrate in place, fail-fast outside the supported range), the
`rebuild_table` template, and the `invalidate-coverage` CLI round-trip. All are
offline against a tmp SQLite file; no provider or network is touched.
"""

from __future__ import annotations

import io
import sqlite3
from datetime import date
from pathlib import Path

import pytest

import baibai_engine.market.sqlite.schema as schema
from baibai_engine.market.sqlite import (
    BASELINE_VERSION,
    LATEST_VERSION,
    SQLITE_SCHEMA_VERSION,
    Migration,
    SQLiteSchemaError,
    connect_current,
    open_connection,
    range_covered,
    rebuild_table,
)
from baibai_engine.screening.cli import invalidate_coverage_command
from tests.helpers.screening_sqlite import add_source_coverage


def _user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)


def _seed_bar(conn: sqlite3.Connection, ticker: str, traded_at: str, close: float) -> None:
    conn.execute(
        "INSERT INTO jquants_daily_bars(ticker, traded_at, close, adjustment_factor) "
        "VALUES (?, ?, ?, 1.0)",
        (ticker, traded_at, close),
    )


# --------------------------------------------------------------------------- #
# create / open
# --------------------------------------------------------------------------- #


def test_open_connection_creates_fresh_store_at_latest_version(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "market.sqlite")
    try:
        assert _user_version(conn) == SQLITE_SCHEMA_VERSION == LATEST_VERSION
    finally:
        conn.close()


def test_open_connection_reopens_latest_store_without_refetch(tmp_path: Path) -> None:
    # With the current empty migration list, a v13 (baseline == latest) store must
    # reopen green and keep its rows -- no delete-and-rebuild.
    sqlite_path = tmp_path / "market.sqlite"
    conn = open_connection(sqlite_path)
    _seed_bar(conn, "1301", "2024-06-28", 100.0)
    conn.commit()
    conn.close()

    reopened = open_connection(sqlite_path)
    try:
        assert _user_version(reopened) == SQLITE_SCHEMA_VERSION
        assert (
            reopened.execute(
                "SELECT close FROM jquants_daily_bars WHERE ticker = '1301'"
            ).fetchone()[0]
            == 100.0
        )
    finally:
        reopened.close()


# --------------------------------------------------------------------------- #
# forward migration (dummy v14 injected into the real mechanism)
# --------------------------------------------------------------------------- #


def test_forward_migration_applies_in_place_and_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    conn = open_connection(sqlite_path)
    _seed_bar(conn, "1301", "2024-06-28", 100.0)
    add_source_coverage(
        conn,
        source="jquants_daily_bars",
        coverage_key="k",
        record_count=5,
        min_date="2024-01-01",
        max_date="2024-06-30",
    )
    conn.commit()
    conn.close()

    # A shape-neutral v14: it transforms existing rows, proving the upgrade runs in
    # place (rows survive) rather than recreating the store. Shape is unchanged, so
    # the strict shape check still passes at the new version.
    dummy = Migration(
        version=LATEST_VERSION + 1,
        statements=("UPDATE source_coverage SET record_count = record_count + 100",),
    )
    monkeypatch.setattr(schema, "MIGRATIONS", (dummy,))
    monkeypatch.setattr(schema, "SQLITE_SCHEMA_VERSION", LATEST_VERSION + 1)

    migrated = open_connection(sqlite_path)
    try:
        assert _user_version(migrated) == LATEST_VERSION + 1
        # Pre-existing bar survived (no re-fetch), and the migration ran on the
        # existing coverage row (5 + 100), proving an in-place transform.
        assert (
            migrated.execute(
                "SELECT close FROM jquants_daily_bars WHERE ticker = '1301'"
            ).fetchone()[0]
            == 100.0
        )
        assert (
            migrated.execute(
                "SELECT record_count FROM source_coverage WHERE coverage_key = 'k'"
            ).fetchone()[0]
            == 105
        )
    finally:
        migrated.close()


def test_forward_migration_sequence_gap_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    open_connection(sqlite_path).close()  # baseline store at LATEST_VERSION

    # A migration two steps ahead leaves a gap the applier must refuse.
    gap = Migration(version=LATEST_VERSION + 2, statements=())
    monkeypatch.setattr(schema, "MIGRATIONS", (gap,))
    monkeypatch.setattr(schema, "SQLITE_SCHEMA_VERSION", LATEST_VERSION + 2)

    with pytest.raises(SQLiteSchemaError, match="sequence gap"):
        open_connection(sqlite_path)


# --------------------------------------------------------------------------- #
# fail-fast outside the supported range
# --------------------------------------------------------------------------- #


def test_open_connection_fails_fast_below_baseline(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    open_connection(sqlite_path).close()
    conn = sqlite3.connect(sqlite_path)
    conn.execute(f"PRAGMA user_version = {BASELINE_VERSION - 1}")
    conn.commit()
    conn.close()

    with pytest.raises(SQLiteSchemaError, match="unsupported screening SQLite schema"):
        open_connection(sqlite_path)


def test_open_connection_fails_fast_above_latest(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    open_connection(sqlite_path).close()
    conn = sqlite3.connect(sqlite_path)
    conn.execute(f"PRAGMA user_version = {LATEST_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(SQLiteSchemaError, match="unsupported screening SQLite schema"):
        open_connection(sqlite_path)
    # A read-only consumer degrades to None rather than raising on the same store.
    assert connect_current(sqlite_path) is None


# --------------------------------------------------------------------------- #
# rebuild_table template
# --------------------------------------------------------------------------- #


def test_rebuild_table_reshapes_and_preserves_carried_columns(tmp_path: Path) -> None:
    conn = open_connection(tmp_path / "market.sqlite")
    try:
        _seed_bar(conn, "1301", "2024-06-28", 100.0)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        # Rebuild to a narrower shape, dropping every column except three.
        rebuild_table(
            conn,
            table="jquants_daily_bars",
            create_statements=(
                "CREATE TABLE jquants_daily_bars("
                "ticker TEXT NOT NULL, traded_at TEXT NOT NULL, close REAL, "
                "PRIMARY KEY (ticker, traded_at))",
            ),
            columns=("ticker", "traded_at", "close"),
        )
        conn.commit()
        assert conn.execute(
            "SELECT ticker, traded_at, close FROM jquants_daily_bars"
        ).fetchall() == [("1301", "2024-06-28", 100.0)]
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "_migrate_jquants_daily_bars" not in tables
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"table": "bad; DROP", "create_statements": (), "columns": ("a",)}, "table identifier"),
        ({"table": "t", "create_statements": (), "columns": ("a b",)}, "column identifier"),
        ({"table": "t", "create_statements": (), "columns": ()}, "at least one carried column"),
    ],
)
def test_rebuild_table_rejects_unsafe_identifiers(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        rebuild_table(sqlite3.connect(":memory:"), **kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# invalidate-coverage CLI
# --------------------------------------------------------------------------- #


def _seed_coverage(sqlite_path: Path) -> None:
    conn = open_connection(sqlite_path)
    add_source_coverage(
        conn,
        source="jquants_fin_summaries",
        coverage_key="fin",
        record_count=5,
        min_date="2024-01-01",
        max_date="2024-12-31",
    )
    add_source_coverage(
        conn,
        source="jquants_daily_bars",
        coverage_key="bars",
        record_count=9,
        min_date="2024-01-01",
        max_date="2024-12-31",
    )
    conn.commit()
    conn.close()


def test_invalidate_coverage_full_round_trip_makes_source_a_bootstrap_target(
    tmp_path: Path,
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_coverage(sqlite_path)

    conn = open_connection(sqlite_path)
    assert range_covered(conn, "jquants_fin_summaries", date(2024, 2, 1), date(2024, 11, 30))
    conn.close()

    out = io.StringIO()
    rc = invalidate_coverage_command(
        sqlite_path=sqlite_path, source="jquants_fin_summaries", stdout=out
    )
    assert rc == 0
    assert "removing 1 source_coverage row(s)" in out.getvalue()

    conn = open_connection(sqlite_path)
    try:
        # The invalidated source's window is no longer covered, so the coverage gate
        # now reports it as needing a bootstrap re-fetch; the other source is intact.
        assert not range_covered(
            conn, "jquants_fin_summaries", date(2024, 2, 1), date(2024, 11, 30)
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM source_coverage WHERE source = 'jquants_fin_summaries'"
            ).fetchone()[0]
            == 0
        )
        assert range_covered(conn, "jquants_daily_bars", date(2024, 2, 1), date(2024, 11, 30))
    finally:
        conn.close()


def test_invalidate_coverage_window_only_removes_overlapping_rows(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    conn = open_connection(sqlite_path)
    add_source_coverage(
        conn,
        source="jquants_fin_summaries",
        coverage_key="2024",
        record_count=5,
        min_date="2024-01-01",
        max_date="2024-06-30",
    )
    add_source_coverage(
        conn,
        source="jquants_fin_summaries",
        coverage_key="2025",
        record_count=5,
        min_date="2025-01-01",
        max_date="2025-06-30",
    )
    conn.commit()
    conn.close()

    out = io.StringIO()
    rc = invalidate_coverage_command(
        sqlite_path=sqlite_path,
        source="jquants_fin_summaries",
        start=date(2024, 3, 1),
        end=date(2024, 4, 1),
        stdout=out,
    )
    assert rc == 0

    conn = open_connection(sqlite_path)
    try:
        remaining = [
            row[0]
            for row in conn.execute(
                "SELECT coverage_key FROM source_coverage WHERE source = 'jquants_fin_summaries'"
            )
        ]
        assert remaining == ["2025"]
    finally:
        conn.close()


def test_invalidate_coverage_rejects_unknown_source_with_known_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_coverage(sqlite_path)

    rc = invalidate_coverage_command(sqlite_path=sqlite_path, source="bogus")
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown coverage source 'bogus'" in err
    assert "jquants_daily_bars" in err
    assert "jquants_fin_summaries" in err


def test_invalidate_coverage_missing_store_is_rejected(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = invalidate_coverage_command(
        sqlite_path=tmp_path / "absent.sqlite", source="jquants_daily_bars"
    )
    assert rc == 1
    assert "SQLite cache not found" in capsys.readouterr().err


def test_invalidate_coverage_requires_both_window_bounds(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite_path = tmp_path / "market.sqlite"
    _seed_coverage(sqlite_path)

    rc = invalidate_coverage_command(
        sqlite_path=sqlite_path, source="jquants_daily_bars", start=date(2024, 1, 1)
    )
    assert rc == 1
    assert "--start and --end must be given together" in capsys.readouterr().err
