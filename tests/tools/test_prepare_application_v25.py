"""The temporary cutover changes schema, not the user's recorded decisions."""

import sqlite3
from contextlib import closing

import pytest
from tools.cutover.prepare_application_v25 import prepare

from baibai_engine.storage.application.schema import SCHEMA_SQL


def source_store(path):
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA_SQL)
    connection.execute("CREATE TABLE ledger_market_price(ticker TEXT PRIMARY KEY, payload TEXT)")
    connection.execute("PRAGMA user_version = 24")
    connection.execute("INSERT INTO ledger_market_price VALUES (?, ?)", ("2915", '{"price":2020}'))
    connection.commit()
    return connection


def test_keeps_wal_business_records_and_original_price_cache(tmp_path):
    source, output = tmp_path / "source.sqlite", tmp_path / "candidate.sqlite"
    with closing(source_store(source)) as original:
        original.execute("PRAGMA journal_mode = WAL")
        original.execute(
            "INSERT INTO ledger_event VALUES (1, 'event', '2026-10-15', 0, "
            "'contribution', NULL, NULL, '{}')"
        )
        original.execute(
            "INSERT INTO market_context_assessment VALUES "
            "('context', 1, '2026-10-15', '2026-10-15T00:00:00Z', NULL, '{}')"
        )
        original.execute("INSERT INTO market_context_assessment_head VALUES (1, 'context')")
        original.commit()
        prepare(source, output)
        assert original.execute("PRAGMA user_version").fetchone() == (24,)
        assert original.execute("SELECT COUNT(*) FROM ledger_market_price").fetchone() == (1,)
        with closing(sqlite3.connect(output)) as candidate:
            assert candidate.execute("PRAGMA user_version").fetchone() == (25,)
            tables = original.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'ledger_market_price'"
            ).fetchall()
            for (table,) in tables:
                assert (
                    candidate.execute(f'SELECT * FROM "{table}"').fetchall()
                    == original.execute(f'SELECT * FROM "{table}"').fetchall()
                )
            assert not candidate.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'ledger_market_price'"
            ).fetchall()


@pytest.mark.parametrize("fault", ["wrong_version", "missing_table", "broken_reference"])
def test_refuses_wrong_or_broken_source_without_retaining_a_candidate(tmp_path, fault):
    source, output = tmp_path / "source.sqlite", tmp_path / "candidate.sqlite"
    with closing(source_store(source)) as original:
        if fault == "wrong_version":
            original.execute("PRAGMA user_version = 23")
        elif fault == "missing_table":
            original.execute("DROP TABLE task")
        else:
            original.execute("INSERT INTO market_context_assessment_head VALUES (1, 'missing')")
        original.commit()
    original_bytes = source.read_bytes()
    with pytest.raises(ValueError, match="expected|shape|references"):
        prepare(source, output)
    assert not output.exists()
    assert source.read_bytes() == original_bytes


@pytest.mark.parametrize("same_path", [False, True])
def test_never_overwrites_a_source_or_existing_candidate(tmp_path, same_path):
    source, output = tmp_path / "source.sqlite", tmp_path / "candidate.sqlite"
    source_store(source).close()
    if same_path:
        output = source
    else:
        output.write_bytes(b"existing candidate")
    before = output.read_bytes()
    with pytest.raises(FileExistsError):
        prepare(source, output)
    assert output.read_bytes() == before
