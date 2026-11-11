"""Retained Article L1 has its own shape, not the enclosing market-cache version."""

import gzip
import sqlite3
from contextlib import closing

import pytest
from tests.helpers.article_acquisition import receipt

from baibai_engine.data_collection.receipts.articles import store
from baibai_engine.data_collection.receipts.articles.storage_contracts import ARTICLE_RECEIPTS_DDL
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only as read_market
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError


def packet(path, ddl=ARTICLE_RECEIPTS_DDL):
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(ddl)
        conn.execute("PRAGMA user_version=49")
    return path


def test_old_enclosing_version_reads_exact_article_without_rewriting(tmp_path):
    path = packet(tmp_path / "raw.sqlite")
    original = receipt()
    with closing(sqlite3.connect(path)) as conn, conn:
        store.retain_receipt(conn, original, body=b"synthetic")
    before = path.read_bytes()
    with closing(store.connect_read_only(path)) as conn:
        assert (
            store.read_receipt(conn, original.receipt_id, receipt_sha256=original.sha256)
            == original
        )
        assert store.read_body(conn, original.body_ref) == b"synthetic"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 49
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM article_source_receipts")
    assert path.read_bytes() == before
    with pytest.raises(SQLiteSchemaError, match="expected=51"):
        read_market(path)


@pytest.mark.parametrize(
    "change", ["missing", "column", "type", "nullable", "primary_key", "hidden", "view"]
)
def test_unknown_article_shapes_are_rejected(tmp_path, change):
    ddl = ARTICLE_RECEIPTS_DDL
    if change == "missing":
        ddl = "CREATE TABLE unrelated (id TEXT);"
    elif change == "column":
        ddl = ddl.replace("body_gzip BLOB", "body_gzip BLOB, extra TEXT")
    elif change == "type":
        ddl = ddl.replace("body_gzip BLOB", "body_gzip TEXT")
    elif change == "nullable":
        ddl = ddl.replace("source TEXT NOT NULL", "source TEXT")
    elif change == "primary_key":
        ddl = ddl.replace(" NOT NULL PRIMARY KEY", " NOT NULL")
    elif change == "hidden":
        ddl = ddl.replace(
            "body_gzip BLOB", "body_gzip BLOB, extra TEXT GENERATED ALWAYS AS (source) VIRTUAL"
        )
    else:
        ddl = "CREATE TABLE originals (receipt_id TEXT); CREATE VIEW article_source_receipts AS SELECT * FROM originals;"
    path = packet(tmp_path / "raw.sqlite", ddl)
    before = path.read_bytes()
    with pytest.raises(store.ArticleStoreError, match="table shape"):
        store.connect_read_only(path)
    assert path.read_bytes() == before


def test_failed_shape_validation_closes_connection(tmp_path, monkeypatch):
    path = packet(tmp_path / "raw.sqlite", "CREATE TABLE other (id TEXT);")
    conn = sqlite3.connect(path)
    monkeypatch.setattr(store, "connect_sqlite_read_only", lambda _: conn)
    with pytest.raises(store.ArticleStoreError):
        store.connect_read_only(path)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


@pytest.mark.parametrize("corruption", ["receipt_digest", "body_digest"])
def test_native_reader_does_not_accept_corrupt_payload(tmp_path, corruption):
    path = packet(tmp_path / "raw.sqlite")
    original = receipt()
    with closing(sqlite3.connect(path)) as conn, conn:
        store.retain_receipt(conn, original, body=b"synthetic")
        if corruption == "receipt_digest":
            conn.execute("UPDATE article_source_receipts SET receipt_sha256=?", ("0" * 64,))
        else:
            conn.execute(
                "UPDATE article_source_receipts SET body_gzip=?", (gzip.compress(b"changed"),)
            )
    before = path.read_bytes()
    with (
        closing(store.connect_read_only(path)) as conn,
        pytest.raises(store.ArticleStoreError, match=r"(identity|digest) mismatch"),
    ):
        store.read_body(conn, original.body_ref)
    assert path.read_bytes() == before
