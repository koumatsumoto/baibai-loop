"""The attended schema copy preserves every old value and never mutates its source."""

import sqlite3
from contextlib import closing

import pytest

from baibai_engine.storage.observations.schema import validate_current_schema
from tools.cutover.upgrade_market_v48 import prior_schema_sql, upgrade


def old_store(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript(prior_schema_sql() + "\nPRAGMA user_version=47;")
        connection.execute("INSERT INTO jquants_market_calendar VALUES ('2026-10-22', 1)")
        connection.execute(
            "INSERT INTO lake_store_origin VALUES (1, 'synthetic-release', ?)", ("a" * 64,)
        )
        connection.execute(
            "INSERT INTO equity_web_source_responses VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "page",
                "attempt",
                "monex",
                "7203",
                "page",
                None,
                "https://synthetic.invalid",
                "2026-10-22T00:00:00+00:00",
                "2026-10-22",
                "utf-8",
                b"original bytes",
            ),
        )
        connection.commit()


def test_lossless_copy_keeps_source_and_adds_empty_forecast_table(tmp_path):
    source, output = tmp_path / "source.sqlite", tmp_path / "output.sqlite"
    old_store(source)
    before = source.read_bytes()
    counts = upgrade(source, output)
    assert source.read_bytes() == before
    assert counts["equity_web_source_responses"] == 1
    with closing(sqlite3.connect(output)) as connection:
        validate_current_schema(connection)
        assert connection.execute(
            "SELECT body_gzip FROM equity_web_source_responses"
        ).fetchone() == (b"original bytes",)
        assert connection.execute("SELECT * FROM lake_store_origin").fetchone() == (
            1,
            "synthetic-release",
            "a" * 64,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM external_assessment_analyst_forecast_observations"
        ).fetchone() == (0,)
        connection.execute(
            "UPDATE equity_web_source_responses SET source='minkabu' WHERE receipt_id='page'"
        )
    with pytest.raises(FileExistsError):
        upgrade(source, output)


@pytest.mark.parametrize("damage", ["version", "extra_table", "changed_column", "missing_table"])
def test_unreviewed_source_stays_untouched_and_no_candidate_remains(tmp_path, damage):
    source, output = tmp_path / "source.sqlite", tmp_path / "output.sqlite"
    old_store(source)
    with closing(sqlite3.connect(source)) as connection:
        connection.execute(
            {
                "version": "PRAGMA user_version=46",
                "extra_table": "CREATE TABLE extra(value TEXT)",
                "changed_column": "ALTER TABLE jquants_market_calendar ADD COLUMN extra TEXT",
                "missing_table": "DROP TABLE jquants_market_calendar",
            }[damage]
        )
        connection.commit()
    before = source.read_bytes()
    with pytest.raises(ValueError):
        upgrade(source, output)
    assert source.read_bytes() == before
    assert not output.exists()
