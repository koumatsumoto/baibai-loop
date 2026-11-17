"""Coverage invalidation edits an existing cache, never initializes a replacement."""

from __future__ import annotations

import io
import sqlite3
from contextlib import closing
from datetime import date

import pytest
from tests.helpers.screening_sqlite import add_source_coverage

from baibai_engine.storage.sqlite.source_cache.schema import SQLITE_SCHEMA_VERSION, open_connection
from baibai_jobs.maintenance.market import cache, cli


@pytest.mark.parametrize("shape", ["empty", "obsolete", "incomplete", "corrupt"])
def test_invalidate_rejects_invalid_store_without_changing_it(tmp_path, monkeypatch, capsys, shape):
    database = tmp_path / "market.sqlite"
    if shape == "empty":
        database.touch()
    elif shape == "corrupt":
        database.write_bytes(b"not a SQLite database")
    else:
        with closing(sqlite3.connect(database)) as conn:
            version = SQLITE_SCHEMA_VERSION if shape == "incomplete" else SQLITE_SCHEMA_VERSION - 1
            conn.execute(f"PRAGMA user_version = {version}")
    before = database.read_bytes()
    entries = {p.name for p in tmp_path.iterdir()}
    connect = sqlite3.connect
    opened = []

    def track(database, *args, **kwargs):
        conn = connect(database, *args, **kwargs)
        if database != ":memory:":
            opened.append(conn)
        return conn

    monkeypatch.setattr(cache.sqlite3, "connect", track)
    out = io.StringIO()
    assert (
        cache.invalidate_coverage_command(sqlite_path=database, source="fixture", stdout=out) == 1
    )
    assert database.read_bytes() == before
    assert out.getvalue() == ""
    assert "unknown coverage source" not in capsys.readouterr().err
    assert len(opened) == 1
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            conn.execute("SELECT 1")
    assert {p.name for p in tmp_path.iterdir()} == entries


def test_invalidate_does_not_recreate_store_that_disappears_after_precheck(tmp_path, monkeypatch):
    database = tmp_path / "market.sqlite"
    moved = tmp_path / "preserved.sqlite"
    with closing(open_connection(database)) as conn:
        add_source_coverage(conn, source="fixture", coverage_key="saved")
        conn.commit()
    before = database.read_bytes()
    entries = {p.name for p in tmp_path.iterdir()}
    connect = sqlite3.connect

    def move_before_connect(target, *args, **kwargs):
        if target != ":memory:" and database.exists():
            database.rename(moved)
        return connect(target, *args, **kwargs)

    monkeypatch.setattr(cache.sqlite3, "connect", move_before_connect)
    out = io.StringIO()
    assert (
        cache.invalidate_coverage_command(sqlite_path=database, source="fixture", stdout=out) == 1
    )
    assert not database.exists()
    assert moved.read_bytes() == before
    assert out.getvalue() == ""
    assert {p.name for p in tmp_path.iterdir()} == (entries - {database.name}) | {moved.name}


@pytest.mark.parametrize("window", [False, True])
def test_invalidate_preserves_other_coverage_and_observations(tmp_path, window):
    database = tmp_path / "market # ?.sqlite"
    with closing(open_connection(database)) as conn:
        for source, key, low, high in (
            ("fixture", "current", "2026-01-01", "2026-01-31"),
            ("fixture", "old", "2025-01-01", "2025-01-31"),
            ("other", "current", "2026-01-01", "2026-01-31"),
        ):
            add_source_coverage(conn, source=source, coverage_key=key, min_date=low, max_date=high)
        conn.execute(
            "INSERT INTO jquants_daily_bars(ticker, traded_at, close) VALUES (?, ?, ?)",
            ("7203", "2026-01-15", 123.0),
        )
        conn.commit()
        bars = conn.execute("SELECT * FROM jquants_daily_bars").fetchall()
        schema = conn.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall()
        other = conn.execute("SELECT * FROM source_coverage WHERE source = 'other'").fetchall()
    out = io.StringIO()
    assert (
        cache.invalidate_coverage_command(
            sqlite_path=database,
            source="fixture",
            start=date(2026, 1, 10) if window else None,
            end=date(2026, 1, 20) if window else None,
            stdout=out,
        )
        == 0
    )
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT * FROM jquants_daily_bars").fetchall() == bars
        assert conn.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall() == schema
        assert (
            conn.execute("SELECT * FROM source_coverage WHERE source = 'other'").fetchall() == other
        )
        assert conn.execute(
            "SELECT coverage_key FROM source_coverage WHERE source = 'fixture'"
        ).fetchall() == ([("old",)] if window else [])
    assert f"removing {1 if window else 2} source_coverage row(s)" in out.getvalue()
    assert "stored observations unchanged" in out.getvalue()


def test_cli_missing_store_stops_without_provider_or_parent_creation(tmp_path, monkeypatch, capsys):
    database = tmp_path / "missing" / "market.sqlite"

    def unexpected(*args, **kwargs):
        raise AssertionError("provider or configuration accessed")

    monkeypatch.setattr(cli, "load_project_env", lambda: None)
    monkeypatch.setattr(cli, "EDINETRefresh", unexpected)
    monkeypatch.setattr(cli.SourceMaintenanceConfig, "from_env", unexpected)
    assert (
        cli.main(["invalidate-coverage", "--source", "fixture", "--sqlite-path", str(database)])
        == 1
    )
    assert not database.parent.exists()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SQLite cache not found" in captured.err
