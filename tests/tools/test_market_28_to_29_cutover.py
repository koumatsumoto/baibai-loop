"""Synthetic schema-28 backup, all-table retention and refusal/failure boundaries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from tools.cutover import market_28_to_29 as cutover

from baibai_engine.market.sqlite import schema


def source28(path: Path) -> Path:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(schema._SCHEMA_SQL)
        for table in cutover._NEW_TABLES:
            connection.execute(f'DROP TABLE "{table}"')
        connection.execute("PRAGMA user_version = 28")
        tables = connection.execute("SELECT name FROM sqlite_schema WHERE type='table'")
        for (table,) in tables.fetchall():
            fields = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            values = {
                name: 1 if kind == "INTEGER" else 101.5 if kind == "REAL" else "synthetic"
                for _, name, kind, required, _, pk in fields
                if required or pk
            }
            special = {
                "ticker": "1234",
                "week_end": "2026-09-18",
                "balance_date": "2026-09-25",
                "status": "disclosed" if table == "tse_capital_policy_snapshots" else "ok",
                "fetch_status": "ok",
                "singleton": 1,
                "release_manifest_sha256": "a" * 64,
            }
            values.update({name: value for name, value in special.items() if name in values})
            connection.execute(
                f'INSERT INTO "{table}"({",".join(values)}) VALUES '
                f"({','.join('?' for _ in values)})",
                tuple(values.values()),
            )
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES('1234','2026-09-30',123.5)"
        )
        connection.commit()
    finally:
        connection.close()
    return path


def evidence(path: Path):
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        catalog = cutover._catalog(connection)
        tables = sorted(name for kind, name in catalog if kind == "table")
        return catalog, {table: cutover._table_evidence(connection, table) for table in tables}
    finally:
        connection.close()


def sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_separate_copy_retains_every_old_table_schema_index_and_row(tmp_path):
    source = source28(tmp_path / "source28.sqlite")
    output = tmp_path / "output29.sqlite"
    before_sha, (before_catalog, before_rows) = sha(source), evidence(source)
    result = cutover.migrate_market_28_to_29(source, output)
    after_catalog, after_rows = evidence(output)
    assert result.source_version == 28
    assert result.output_version == 29
    assert result.all_old_rows_equal
    assert result.all_old_schema_equal
    assert result.integrity_ok
    assert result.foreign_keys_ok
    assert len(result.preserved_tables) == 22
    assert result.preserved_tables == before_rows
    for name, row in before_rows.items():
        assert after_rows[name] == row
        assert row.rows > 0
    assert {
        key: value for key, value in after_catalog.items() if value[0] not in cutover._NEW_TABLES
    } == before_catalog
    assert all(after_rows[table].rows == 0 for table in cutover._NEW_TABLES)
    assert sha(source) == before_sha
    assert output.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(output) as connection:
        schema.validate_current_schema(connection)
    with pytest.raises(schema.SQLiteSchemaError, match="obsolete"):
        schema.open_connection(source)
    assert sha(source) == before_sha


def test_backup_includes_committed_wal_and_excludes_uncommitted_rows(tmp_path):
    source = source28(tmp_path / "wal28.sqlite")
    output = tmp_path / "wal29.sqlite"
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close) "
            "VALUES('1234','2026-10-01',234.5)"
        )
        writer.commit()
        writer.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close) "
            "VALUES('1234','2026-10-02',345.5)"
        )
        wal = Path(str(source) + "-wal")
        before_sha, before_wal_sha = sha(source), sha(wal)
        result = cutover.migrate_market_28_to_29(source, output)
        assert result.preserved_tables["jquants_daily_bars"].rows == 3
        assert sha(source) == before_sha
        assert sha(wal) == before_wal_sha
        assert writer.in_transaction
        with sqlite3.connect(output) as connection:
            assert connection.execute(
                "SELECT close FROM jquants_daily_bars WHERE traded_at='2026-10-01'"
            ).fetchone() == (234.5,)
            assert (
                connection.execute(
                    "SELECT close FROM jquants_daily_bars WHERE traded_at='2026-10-02'"
                ).fetchone()
                is None
            )
    finally:
        writer.rollback()
        writer.close()


def test_existing_output_same_path_and_repeated_cutover_never_overwrite(tmp_path):
    source = source28(tmp_path / "source28.sqlite")
    original_sha = sha(source)
    with pytest.raises(ValueError, match="separate"):
        cutover.migrate_market_28_to_29(source, source)
    existing = tmp_path / "existing.sqlite"
    existing.write_bytes(b"existing output stays untouched")
    with pytest.raises(FileExistsError):
        cutover.migrate_market_28_to_29(source, existing)
    assert existing.read_bytes() == b"existing output stays untouched"
    output = tmp_path / "output29.sqlite"
    cutover.migrate_market_28_to_29(source, output)
    output_sha = sha(output)
    with pytest.raises(FileExistsError):
        cutover.migrate_market_28_to_29(source, output)
    with pytest.raises(schema.SQLiteSchemaError, match="exactly schema 28"):
        cutover.migrate_market_28_to_29(output, tmp_path / "another.sqlite")
    assert sha(source) == original_sha
    assert sha(output) == output_sha
    assert not (tmp_path / "another.sqlite").exists()


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
@pytest.mark.parametrize("dangling_link", [False, True])
def test_existing_output_prefix_files_and_dangling_links_stay_untouched(
    tmp_path,
    suffix,
    dangling_link,
):
    source = source28(tmp_path / "source28.sqlite")
    output = tmp_path / "output29.sqlite"
    existing = Path(str(output) + suffix)
    if dangling_link:
        existing.symlink_to("missing-private-sidecar")
    else:
        existing.write_bytes(b"existing prefix file must survive")
    before_source = sha(source)
    with pytest.raises(FileExistsError, match="prefix already exists"):
        cutover.migrate_market_28_to_29(source, output)
    assert sha(source) == before_source
    if dangling_link:
        assert existing.is_symlink()
        assert existing.readlink() == Path("missing-private-sidecar")
    else:
        assert existing.read_bytes() == b"existing prefix file must survive"
    assert not list(tmp_path.glob(".market-28-to-29-*"))


@pytest.mark.parametrize("fail_validation", [False, True])
def test_sidecar_created_during_private_conversion_survives_failure(
    tmp_path,
    monkeypatch,
    fail_validation,
):
    source = source28(tmp_path / "source28.sqlite")
    output = tmp_path / "output29.sqlite"
    sidecar = Path(str(output) + "-wal")
    before_source = sha(source)
    validate = cutover.schema.validate_current_schema

    def create_external_sidecar(connection):
        sidecar.write_bytes(b"external sidecar created while conversion runs")
        if fail_validation:
            raise schema.SQLiteSchemaError("synthetic validation failure")
        validate(connection)

    monkeypatch.setattr(cutover.schema, "validate_current_schema", create_external_sidecar)
    with pytest.raises((schema.SQLiteSchemaError, FileExistsError)):
        cutover.migrate_market_28_to_29(source, output)
    assert sidecar.read_bytes() == b"external sidecar created while conversion runs"
    assert not output.exists()
    assert sha(source) == before_source
    assert not list(tmp_path.glob(".market-28-to-29-*"))


def test_atomic_install_race_does_not_overwrite_or_delete_competing_output(tmp_path, monkeypatch):
    source = source28(tmp_path / "source28.sqlite")
    output = tmp_path / "output29.sqlite"
    before_source = sha(source)
    link = cutover.os.link

    def competing_output(staged, final):
        final.write_bytes(b"competing output appears after final absence check")
        link(staged, final)

    monkeypatch.setattr(cutover.os, "link", competing_output)
    with pytest.raises(FileExistsError):
        cutover.migrate_market_28_to_29(source, output)
    assert output.read_bytes() == b"competing output appears after final absence check"
    assert sha(source) == before_source
    assert not list(tmp_path.glob(".market-28-to-29-*"))


@pytest.mark.parametrize("malformation", ["index", "constraint", "extra_table", "wrong_version"])
def test_source_schema_mismatch_refused_before_output_creation(tmp_path, malformation):
    source = source28(tmp_path / "source28.sqlite")
    output = tmp_path / "output29.sqlite"
    with sqlite3.connect(source) as connection:
        if malformation == "index":
            connection.execute("DROP INDEX idx_jquants_daily_bars_traded_at")
            connection.execute(
                "CREATE INDEX idx_jquants_daily_bars_traded_at ON jquants_daily_bars(ticker)"
            )
        elif malformation == "constraint":
            sql = connection.execute(
                "SELECT sql FROM sqlite_schema WHERE name='jquants_weekly_margin'"
            ).fetchone()[0]
            connection.execute("DROP TABLE jquants_weekly_margin")
            connection.execute(sql.replace(" CHECK (week_end <= '2026-09-18')", ""))
            connection.execute(
                "CREATE INDEX idx_jquants_weekly_margin_ticker ON jquants_weekly_margin(ticker,week_end)"
            )
        elif malformation == "extra_table":
            connection.execute("CREATE TABLE unexpected(value TEXT)")
        else:
            connection.execute("PRAGMA user_version=27")
    before = sha(source)
    with pytest.raises(schema.SQLiteSchemaError):
        cutover.migrate_market_28_to_29(source, output)
    assert not output.exists()
    assert sha(source) == before


def test_equivalent_indentation_and_comments_accept_but_raw_ddl_stays_preserved(tmp_path):
    source = source28(tmp_path / "source28.sqlite")
    output = tmp_path / "output29.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("DROP INDEX idx_jquants_daily_bars_traded_at")
        connection.execute(
            'CREATE INDEX "idx_jquants_daily_bars_traded_at" /* old attended DDL */\n'
            "ON jquants_daily_bars(\n traded_at\n)"
        )
    original_catalog, _ = evidence(source)
    result = cutover.migrate_market_28_to_29(source, output)
    assert result.all_old_schema_equal
    output_catalog, _ = evidence(output)
    assert (
        output_catalog[("index", "idx_jquants_daily_bars_traded_at")]
        == original_catalog[("index", "idx_jquants_daily_bars_traded_at")]
    )


def test_sql_literals_with_comment_markers_are_preserved_in_schema_comparison():
    key = ("table", "synthetic")
    first = {key: ("synthetic", "CREATE TABLE synthetic(value TEXT DEFAULT '-- original /*')")}
    second = {key: ("synthetic", "CREATE TABLE synthetic(value TEXT DEFAULT '-- changed /*')")}
    assert cutover._schema_tokens(first) != cutover._schema_tokens(second)


def test_corrupt_and_absent_source_refused_without_output(tmp_path):
    source, output = tmp_path / "corrupt.sqlite", tmp_path / "output29.sqlite"
    with pytest.raises(FileNotFoundError):
        cutover.migrate_market_28_to_29(source, output)
    assert not source.exists()
    source.write_bytes(b"not a SQLite database")
    with pytest.raises(sqlite3.DatabaseError):
        cutover.migrate_market_28_to_29(source, output)
    assert source.read_bytes() == b"not a SQLite database"
    assert not output.exists()


def test_post_backup_failure_removes_only_new_output(tmp_path, monkeypatch):
    source, output = source28(tmp_path / "source28.sqlite"), tmp_path / "output29.sqlite"
    before = sha(source)

    def fail(_connection):
        raise schema.SQLiteSchemaError("synthetic post-backup validation failure")

    monkeypatch.setattr(cutover.schema, "validate_current_schema", fail)
    with pytest.raises(schema.SQLiteSchemaError, match="post-backup"):
        cutover.migrate_market_28_to_29(source, output)
    assert sha(source) == before
    assert not output.exists()
    assert not Path(str(output) + "-wal").exists()
    assert not Path(str(output) + "-shm").exists()
    assert not Path(str(output) + "-journal").exists()


def test_cli_requires_explicit_paths_and_emits_only_hash_count_evidence(tmp_path, capsys):
    source = source28(tmp_path / "source28.sqlite")
    output = tmp_path / "output29.sqlite"
    with pytest.raises(SystemExit):
        cutover.main(["--source", str(source)])
    assert cutover.main(["--source", str(source), "--output", str(output)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["all_old_rows_equal"]
    assert report["all_old_schema_equal"]
    assert report["preserved_tables"]["lake_store_origin"]["rows"] == 1
    assert len(report["preserved_tables"]["source_coverage"]["sha256"]) == 64
