"""Scalar history cannot be relabeled as freshly acquired native observations."""

import sqlite3
from contextlib import closing

import pytest

from baibai_engine.market_context.indicators.db import SCHEMA_PATH
from tools.cutover.prepare_indicators_v9 import prepare


def source_store(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("DROP TABLE indicator_source_snapshots")
    conn.execute("PRAGMA user_version = 7")
    conn.execute("UPDATE registry_state SET generation = 42")
    conn.execute(
        "INSERT INTO series(series_id, name, category, geography, frequency, unit, "
        "provider, provider_series_id, source_id, source_url) "
        "VALUES ('test', 'test', 'test', 'JP', 'daily', 'percent', 'test', 'test', 'test', 'test')"
    )
    conn.execute(
        "INSERT INTO observations VALUES "
        "('test', '2026-10-14', NULL, NULL, 12.5, 'percent', '2026-10-15T00:00:00Z', 'ok', 'test')"
    )
    conn.commit()
    return conn


def test_preserves_scalar_history_and_registry_without_inventing_native_rows(tmp_path):
    source, output = tmp_path / "source.sqlite", tmp_path / "candidate.sqlite"
    with closing(source_store(source)) as original:
        prepare(source, output)
        assert original.execute("PRAGMA user_version").fetchone() == (7,)
        with closing(sqlite3.connect(output)) as candidate:
            assert candidate.execute("PRAGMA user_version").fetchone() == (9,)
            assert candidate.execute("SELECT COUNT(*) FROM indicator_source_snapshots").fetchone() == (0,)
            for (table,) in original.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
                assert candidate.execute(f'SELECT * FROM "{table}"').fetchall() == original.execute(
                    f'SELECT * FROM "{table}"'
                ).fetchall()


@pytest.mark.parametrize("fault", ["wrong_version", "missing_table", "missing_registry"])
def test_refuses_unexpected_source_without_changing_it(tmp_path, fault):
    source, output = tmp_path / "source.sqlite", tmp_path / "candidate.sqlite"
    with closing(source_store(source)) as original:
        if fault == "wrong_version":
            original.execute("PRAGMA user_version = 8")
        elif fault == "missing_table":
            original.execute("DROP TABLE provider_runs")
        else:
            original.execute("DELETE FROM registry_state")
        original.commit()
    before = source.read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        prepare(source, output)
    assert not output.exists()
    assert source.read_bytes() == before


def test_does_not_overwrite_an_existing_output(tmp_path):
    source = tmp_path / "source.sqlite"
    source_store(source).close()
    before = source.read_bytes()
    with pytest.raises(FileExistsError):
        prepare(source, source)
    assert source.read_bytes() == before
