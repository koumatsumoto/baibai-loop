from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from tests.helpers.lake_release import market_store, publish_market_fixture

from baibai_engine.storage.snapshots import transfer
from baibai_engine.storage.snapshots.hydrate import LakeHydrateError, dehydrate_market_store
from baibai_engine.storage.snapshots.objects import LocalMirrorSource
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.sqlite.snapshot import create_snapshot
from baibai_engine.storage.sqlite.source_cache.lake_origin import read_lake_store_origin
from baibai_jobs.publication.datasets import LAKE_DATASETS


@pytest.fixture
def inputs(tmp_path):
    source = market_store(tmp_path / "working.sqlite", short_sale_reports=())
    publish_market_fixture(source, "transfer-check", created_at=datetime(2026, 5, 1, tzinfo=UTC))
    origin = read_lake_store_origin(source)
    assert origin is not None
    release = resolve_release(
        LocalMirrorSource(tmp_path / "mirror"),
        origin.release_id,
        manifest_sha256=origin.release_manifest_sha256,
    )
    return source, {"release": release, "catalog": LAKE_DATASETS}


def projection(path):
    with closing(sqlite3.connect(path)) as db:
        schema = db.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name"
        ).fetchall()
        rows = {
            name: db.execute(f'SELECT rowid,* FROM "{name}" ORDER BY rowid').fetchall()
            for kind, name, _, _ in schema
            if kind == "table"
        }
        assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        return (
            schema,
            rows,
            db.execute("PRAGMA user_version").fetchone(),
            db.execute("PRAGMA application_id").fetchone(),
            db.execute("PRAGMA journal_mode").fetchone(),
        )


def test_normal_copy_is_logically_the_old_transfer_and_never_backs_up_the_full_db(
    inputs, tmp_path, monkeypatch
):
    source, kwargs = inputs
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("PRAGMA application_id=12345")
        db.execute(
            "UPDATE source_coverage SET error=? WHERE rowid=(SELECT min(rowid) FROM source_coverage)",
            (b"\0\xff\x01",),
        )
    before = source.read_bytes()
    old, new = tmp_path / "old", tmp_path / "new"
    create_snapshot(source, old)
    expected = dehydrate_market_store(old, **kwargs)
    monkeypatch.setattr(
        transfer, "backup_to_new_file", lambda *_: pytest.fail("full copy on normal schema")
    )
    report = transfer.make_market_transfer(source, new, **kwargs)
    assert report.removed_rows == expected.removed_rows
    assert report.bytes_after == expected.bytes_after
    assert projection(new) == projection(old)
    assert source.read_bytes() == before
    assert not tuple(tmp_path.glob(".market-transfer-*"))


@pytest.mark.parametrize(
    "change", ["extra-table", "sequence", "trigger", "noncanonical-index", "unknown-version"]
)
def test_special_schema_uses_lossless_sqlite_backup(inputs, tmp_path, monkeypatch, change):
    source, kwargs = inputs
    with closing(sqlite3.connect(source)) as db, db:
        if change == "extra-table":
            db.execute("CREATE TABLE notes(k TEXT PRIMARY KEY,v BLOB)")
            db.execute("INSERT INTO notes VALUES('retained', X'00807f')")
        elif change == "sequence":
            db.execute("CREATE TABLE ids(id INTEGER PRIMARY KEY AUTOINCREMENT)")
            db.execute("INSERT INTO ids VALUES (91)")
            db.execute("DELETE FROM ids")
        elif change == "trigger":
            db.execute(
                "CREATE TRIGGER third_party AFTER INSERT ON source_coverage BEGIN SELECT 1; END"
            )
        elif change == "noncanonical-index":
            db.execute("DROP INDEX idx_source_coverage_source_window")
            db.execute(
                "CREATE INDEX idx_source_coverage_source_window ON source_coverage(coverage_key)"
            )
        else:
            db.execute("PRAGMA user_version=53")
    old, new = tmp_path / "old", tmp_path / "new"
    create_snapshot(source, old)
    dehydrate_market_store(old, **kwargs)
    stamp = []
    backup = transfer.backup_to_new_file

    def tracked(*args):
        stamp.append(1)
        return backup(*args)

    monkeypatch.setattr(transfer, "backup_to_new_file", tracked)
    transfer.make_market_transfer(source, new, **kwargs)
    assert stamp == [1]
    assert projection(new) == projection(old)


@pytest.mark.parametrize(
    "problem", ["row-count", "unpublished", "origin", "moving-pointer", "verification", "alias"]
)
def test_refusal_never_replaces_last_good_output_or_mutates_working_file(inputs, tmp_path, problem):
    source, kwargs = inputs
    if problem in ("row-count", "unpublished", "origin"):
        with closing(sqlite3.connect(source)) as db, db:
            if problem == "row-count":
                db.execute("DELETE FROM jquants_daily_bars WHERE rowid=1")
            elif problem == "unpublished":
                db.execute(
                    "INSERT INTO edinet_list_responses VALUES('unexpected','2026-01-05','2026-01-05T00:00:00.000000+00:00',X'0100')"
                )
            else:
                db.execute("DELETE FROM lake_store_origin")
    if problem == "moving-pointer":
        kwargs["still_current"] = lambda: ("other", "a" * 64)
    if problem == "verification":

        def fail(_):
            raise ValueError("unverified independent source")

        kwargs["prepare_copy"] = fail
    output = tmp_path / "final"
    output.write_bytes(b"last good")
    before = hashlib.sha256(source.read_bytes()).digest()
    with pytest.raises((LakeHydrateError, ValueError)):
        transfer.make_market_transfer(source, source if problem == "alias" else output, **kwargs)
    assert output.read_bytes() == b"last good"
    assert hashlib.sha256(source.read_bytes()).digest() == before
    assert not tuple(tmp_path.glob(".market-transfer-*"))


def test_committed_wal_and_private_independent_receipts_match_previous_transfer(inputs, tmp_path):
    source, kwargs = inputs
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE source_coverage SET error='only in wal'")
        writer.execute(
            "INSERT INTO edinet_list_responses VALUES('id','2026-01-05','2026-01-05T00:00:00.000000+00:00',X'0080')"
        )
        writer.commit()
        wal = Path(f"{source}-wal")
        before = (source.read_bytes(), wal.read_bytes())

        def verify(copy):
            with closing(sqlite3.connect(copy)) as db, db:
                assert db.execute("SELECT body_gzip FROM edinet_list_responses").fetchall() == [
                    (b"\0\x80",)
                ]
                db.execute("DELETE FROM edinet_list_responses")

        old, new = tmp_path / "old", tmp_path / "new"
        create_snapshot(source, old)
        verify(old)
        dehydrate_market_store(old, **kwargs)
        transfer.make_market_transfer(source, new, prepare_copy=verify, **kwargs)
        assert projection(new) == projection(old)
        assert (source.read_bytes(), wal.read_bytes()) == before
        assert not Path(f"{new}-wal").exists()
