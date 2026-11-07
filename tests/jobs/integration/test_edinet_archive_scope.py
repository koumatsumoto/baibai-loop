"""Requested scope projection is independent of received ZIPs and SQL commit."""

import sqlite3
from contextlib import closing
from datetime import date
from unittest.mock import Mock

import pytest
from tests.jobs.integration.test_edinet_input_storage_contract import source_fixture

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.data_collection.providers.edinet.facts_scope import (
    FactsScope,
    merge_scope,
    read_scope,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration import edinet_archive_native as owner


@pytest.mark.parametrize(
    "existing",
    [
        FactsScope(),
        FactsScope(date(2024, 1, 1), frozenset({"S100EARLY"}), frozenset({"b" * 64})),
    ],
)
def test_scope_projection_keeps_unreceived_requests_and_monotonic_join_without_body_io(
    tmp_path, monkeypatch, existing
):
    store, _, _, _, archives = source_fixture(tmp_path)
    for key in tuple(store.values):
        if key.endswith(".zip"):
            del store.values[key]
    store.get_keys.clear()
    store.head_keys.clear()
    store.put_keys.clear()
    monkeypatch.setattr(
        FixedEdinetArchives, "read_archive", Mock(side_effect=AssertionError("ZIP body read"))
    )
    database = tmp_path / "scope.sqlite"
    expected = existing.join(archives.scope)
    with closing(open_connection(database)) as connection, connection:
        merge_scope(connection, existing)

        def forbid_archive_read(action, table, column, *_):
            if action == sqlite3.SQLITE_READ and table == "edinet_source_archives":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(forbid_archive_read)
        assert owner.project_archive_scope(archives, connection) == expected
        assert owner.project_archive_scope(archives, connection) == expected
        assert read_scope(connection) == expected
        connection.set_authorizer(None)
        assert "S100MISSING" in expected.doc_ids
        assert ("S100MISSING", 1) not in archives.archive_keys
        for table in (
            "edinet_source_archives",
            "source_coverage",
            "edinet_metrics",
            "edinet_segment_facts",
            "edinet_debt_schedule",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    with closing(open_connection(database)) as connection:
        assert read_scope(connection) == expected
    assert store.get_keys == store.head_keys == store.put_keys == []


def test_scope_projection_does_not_commit_the_callers_transaction(tmp_path):
    _, _, _, _, archives = source_fixture(tmp_path)
    database = tmp_path / "scope.sqlite"
    existing = FactsScope(date(2024, 1, 1), frozenset({"S100EARLY"}))
    with closing(open_connection(database)) as connection, connection:
        merge_scope(connection, existing)
    with closing(open_connection(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        owner.project_archive_scope(archives, connection)
        assert connection.in_transaction
        connection.rollback()
        assert read_scope(connection) == existing


def test_full_materialize_rolls_back_bodies_and_scope_if_projection_fails(tmp_path, monkeypatch):
    _, _, _, _, archives = source_fixture(tmp_path)
    database = tmp_path / "market.sqlite"
    with closing(open_connection(database)):
        pass
    before = database.read_bytes()
    project = owner.project_archive_scope

    def fail_after_scope_merge(fixed, connection):
        assert connection.in_transaction
        assert connection.execute("SELECT count(*) FROM edinet_source_archives").fetchone() == (
            len(archives.archive_keys),
        )
        assert project(fixed, connection) == archives.scope
        raise RuntimeError("projection interrupted")

    projection = Mock(side_effect=fail_after_scope_merge)
    monkeypatch.setattr(owner, "project_archive_scope", projection)
    with pytest.raises(RuntimeError, match="projection interrupted"):
        owner.materialize_archives(archives, database)
    projection.assert_called_once()
    assert database.read_bytes() == before
    with closing(open_connection(database)) as connection:
        assert read_scope(connection) == FactsScope()
        assert connection.execute("SELECT count(*) FROM edinet_source_archives").fetchone() == (0,)
