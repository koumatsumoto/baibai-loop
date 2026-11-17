"""Explicit copy retains every old value; normal readers never migrate."""

import sqlite3
from contextlib import closing

import pytest
from tools.migrations.market_schema_52 import DETAILS, digest, migrate

from baibai_engine.storage.sqlite.source_cache.schema import (
    SQLiteSchemaError,
    connect_current,
)


def test_new_copy_has_unknown_details_and_all_original_rows(schema51, tmp_path):
    before = digest(schema51)
    mtime = schema51.stat().st_mtime_ns
    out = tmp_path / "current.sqlite"
    with pytest.raises(SQLiteSchemaError):
        connect_current(schema51)
    receipt = migrate(schema51, out, expected_sha256=before)
    assert receipt["status"] == "verified_local_schema_copy"
    assert digest(schema51) == before
    assert schema51.stat().st_mtime_ns == mtime
    with closing(connect_current(out)) as c:
        assert c.execute(
            "SELECT fcf_ttm," + ",".join(DETAILS) + " FROM edinet_metrics"
        ).fetchone() == (80, None, None)
        assert c.execute("SELECT * FROM retained_extra").fetchone() == ("native", b"\x00\xffraw")
    assert (tmp_path / "current.sqlite.receipt.json").is_file()
    with pytest.raises(ValueError, match="new output"):
        migrate(schema51, out, expected_sha256=before)


def test_wrong_digest_never_creates_output(schema51, tmp_path):
    out = tmp_path / "wrong.sqlite"
    with pytest.raises(ValueError, match="SHA"):
        migrate(schema51, out, expected_sha256="0" * 64)
    assert not out.exists()
    assert not out.with_name(out.name + ".incomplete").exists()


def test_incomplete_shape_is_not_published(schema51, tmp_path):
    with sqlite3.connect(schema51) as c:
        c.execute("DROP TABLE edinet_documents")
    out = tmp_path / "bad.sqlite"
    with pytest.raises(SQLiteSchemaError):
        migrate(schema51, out, expected_sha256=digest(schema51))
    assert not out.exists()
    assert out.with_name(out.name + ".incomplete").exists()
    with pytest.raises(ValueError, match="previous migration"):
        migrate(schema51, out, expected_sha256=digest(schema51))
