from contextlib import closing
from datetime import UTC, date, datetime

import pytest

from baibai_batch.storage.merge_economic_store import merge_stores
from baibai_batch.storage.store_merge import MergeError
from baibai_engine.collection.economic.documents import SourceDocument, store_document
from baibai_engine.collection.economic.records import store_snapshot
from baibai_engine.collection.economic.store import initialize_database, row_count

DAY = date(2026, 7, 20)
WHEN = datetime(2026, 7, 21, tzinfo=UTC)


def _source(path, value=1):
    with closing(initialize_database(path)) as connection, connection:
        store_snapshot(
            connection,
            source_id="retained-source-not-used-by-l2",
            observed_at=DAY,
            fetched_at=WHEN,
            records=[{"value": value, "unknown": None}],
        )


def _document(path, body):
    with closing(initialize_database(path)) as connection, connection:
        store_document(
            connection,
            SourceDocument(
                "original-document",
                "history",
                "https://example.com/raw",
                "application/json",
                WHEN,
                body,
                {},
            ),
        )


def test_merge_keeps_sources_without_a_current_l2_use_and_is_idempotent(tmp_path):
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    _source(source)
    with closing(initialize_database(target)):
        pass
    assert merge_stores(source, target).inserted == 1
    assert merge_stores(source, target).inserted == 0
    with closing(initialize_database(target)) as connection:
        assert row_count(connection, "economic_source_records") == 1


def test_conflicting_native_payload_is_not_silently_replaced(tmp_path):
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    _source(source, 1)
    _source(target, 2)
    before = target.read_bytes()
    with pytest.raises(MergeError):
        merge_stores(source, target)
    assert target.read_bytes() == before


def test_document_conflict_rolls_back_earlier_raw_record_inserts(tmp_path):
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    _source(source)
    _document(source, b'{"revision": 1}')
    _document(target, b'{"revision": 2}')
    with pytest.raises(MergeError):
        merge_stores(source, target)
    with closing(initialize_database(target)) as connection:
        assert row_count(connection, "economic_source_records") == 0
        assert (
            connection.execute("SELECT payload FROM economic_source_documents").fetchone()[0]
            == b'{"revision": 2}'
        )


def test_conflicting_run_identity_is_not_ignored(tmp_path):
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    for path, status in [(source, "ok"), (target, "failed")]:
        with closing(initialize_database(path)) as connection, connection:
            connection.execute(
                "INSERT INTO economic_source_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "same-run",
                    "fed-h15",
                    str(DAY),
                    str(DAY),
                    WHEN.isoformat(),
                    WHEN.isoformat(),
                    status,
                    1,
                    None,
                ),
            )
    with pytest.raises(MergeError):
        merge_stores(source, target)


def test_merge_refuses_an_unexpected_target_trigger_before_any_write(tmp_path):
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    _source(source)
    _source(target, 2)
    with closing(initialize_database(target)) as connection, connection:
        connection.execute(
            "CREATE TRIGGER erase_history AFTER INSERT ON economic_source_documents "
            "BEGIN DELETE FROM economic_source_records; END"
        )
    before = target.read_bytes()
    with pytest.raises(MergeError, match="unexpected trigger"):
        merge_stores(source, target)
    assert target.read_bytes() == before


def test_merge_refuses_an_old_schema_without_migrating_it(tmp_path):
    source, target = tmp_path / "source.sqlite", tmp_path / "target.sqlite"
    _source(source)
    _source(target)
    with closing(initialize_database(source)) as connection:
        connection.execute("PRAGMA user_version = 11")
    before = target.read_bytes()
    with pytest.raises(MergeError, match="schema"):
        merge_stores(source, target)
    assert target.read_bytes() == before


def test_merge_refuses_a_schema_it_does_not_attach():
    from baibai_batch.storage.store_merge import schema_name

    with pytest.raises(ValueError, match="unsupported SQLite schema name"):
        schema_name("main; DROP TABLE economic_source_records")


def test_merge_refuses_a_table_name_that_is_not_an_identifier():
    from baibai_batch.storage.store_merge import internal_name

    with pytest.raises(ValueError, match="invalid SQL identifier"):
        internal_name('economic_source_records" --')


def test_main_reports_a_missing_store(tmp_path, capsys):
    from baibai_batch.storage.merge_economic_store import main

    target = tmp_path / "target.sqlite"
    with closing(initialize_database(target)):
        pass
    assert main(["--source", str(tmp_path / "absent.sqlite"), "--target", str(target)]) == 1
    assert "does not exist" in capsys.readouterr().err
