"""The attended schema copy preserves every old value and never mutates its source."""

import sqlite3
from contextlib import closing

import pytest
from tools.cutover.upgrade_market_v48 import prior_schema_sql, upgrade

from baibai_engine.storage.observations.schema import validate_current_schema


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
    with pytest.raises(ValueError, match=r"source (must be|schema differs)"):
        upgrade(source, output)
    assert source.read_bytes() == before
    assert not output.exists()


def test_materialize_upgrade_is_explicit_and_ordered():
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/cloud-materialize.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["workflow_dispatch"]["inputs"]["upgrade_market_v48"]["default"] is False
    steps = workflow["jobs"]["materialize"]["steps"]
    prepare = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Prepare attended market schema 48 copy"
    )
    hydrate = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Hydrate market store from the L1 release"
    )
    publish = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Publish attended schema 48 machine bundle"
    )
    render = next(
        i for i, step in enumerate(steps) if step.get("name") == "Materialize read models"
    )
    capture = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Capture three live Minkabu examples before the attended cutover"
    )
    lease = next(
        i for i, step in enumerate(steps) if step.get("name") == "Acquire publication lease"
    )
    originals = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Import and publish the confirmed Minkabu originals"
    )
    assert capture < lease < prepare < hydrate < originals < publish < render
    assert steps[capture]["if"] == steps[originals]["if"] == "${{ inputs.upgrade_market_v48 }}"
    assert steps[prepare]["if"] == steps[publish]["if"] == "${{ inputs.upgrade_market_v48 }}"
