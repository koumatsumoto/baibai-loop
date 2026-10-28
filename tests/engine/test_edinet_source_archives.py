from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, date, datetime
from io import BytesIO
from unittest.mock import patch

import pyarrow.parquet as pq
import pytest
from tests.helpers.edinet_documents import store_edinet_documents
from tests.helpers.http_doubles import ExplodingSession, FakeSession, response
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.data_collection.edinet import http as edinet_http
from baibai_engine.data_collection.edinet.archives import read_source_archive, retain_source_archive
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.market.edinet_reader import EDINETReader
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh
from baibai_engine.storage.lake.datasets import EDINET_SOURCE_ARCHIVES
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import iter_partition_rows, resolve_release
from baibai_engine.storage.lake.release import create_release
from baibai_engine.storage.lake.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.observations.read import connect_read_only
from baibai_engine.storage.observations.schema import open_connection


def _zip(content: bytes) -> bytes:
    target = BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("source/document", content)
    return target.getvalue()


def _read_stored_archive(path, doc_id, source_type):
    with closing(connect_read_only(path)) as connection:
        return read_source_archive(connection, doc_id, source_type)


def _retain_stored_archive(path, doc_id, source_type, content, *, stored_on):
    with closing(open_connection(path)) as connection:
        return retain_source_archive(connection, doc_id, source_type, content, stored_on=stored_on)


@pytest.mark.parametrize(
    ("source_type", "method"), [(1, "download_xbrl_zip"), (5, "download_csv_zip")]
)
def test_downloaded_bytes_are_available_from_selected_store_without_http(
    tmp_path, source_type, method
):
    content = _zip(b"original units, missing facts, and unselected fields")
    reply = response(content)
    provider = EDINETRefresh(
        "test-key",
        session=FakeSession(reply),
        sqlite_path=tmp_path / "market.sqlite",
    )
    assert getattr(provider, method)("S100TEST") == content
    assert reply.close_count == 1
    assert _read_stored_archive(tmp_path / "market.sqlite", "S100TEST", source_type) == content
    offline = EDINETRefresh(
        None,
        session=ExplodingSession(),
        sqlite_path=tmp_path / "market.sqlite",
    )
    assert getattr(offline, method)("S100TEST") == content


@pytest.mark.parametrize(
    ("kind", "source_type", "method"),
    [
        ("csv", 5, "download_csv_zip"),
        ("xbrl", 1, "download_xbrl_zip"),
    ],
)
def test_old_local_zip_is_ignored_and_response_bytes_use_capture_date(
    tmp_path, monkeypatch, kind, source_type, method
):
    old_content = _zip(b"old machine's archive")
    content = _zip(b"selected store's new response")
    cache = tmp_path / "cache" / "edinet" / f"{kind}_zips" / "S100TEST.zip"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(old_content)
    captured = datetime(2026, 2, 3, 23, 59, tzinfo=UTC)

    class CaptureClock:
        @staticmethod
        def now(_tz):
            return captured

    monkeypatch.setattr(edinet_http, "datetime", CaptureClock)
    session = FakeSession(response(content))
    path = tmp_path / "market.sqlite"
    provider = EDINETRefresh(
        "test-key",
        session=session,
        sqlite_path=path,
    )
    assert getattr(provider, method)("S100TEST") == content
    assert len(session.calls) == 1
    assert _read_stored_archive(path, "S100TEST", source_type) == content
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(
            "SELECT stored_on FROM edinet_source_archives WHERE doc_id = ? AND source_type = ?",
            ("S100TEST", source_type),
        ).fetchone() == ("2026-02-03",)
    assert cache.read_bytes() == old_content


@pytest.mark.parametrize(
    ("kind", "method"), [("csv", "download_csv_zip"), ("xbrl", "download_xbrl_zip")]
)
def test_failed_archive_fetch_does_not_import_old_local_zip(tmp_path, kind, method):
    old_content = _zip(b"old machine's archive")
    cache = tmp_path / "cache" / "edinet" / f"{kind}_zips" / "S100TEST.zip"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(old_content)
    path = tmp_path / "selected.sqlite"
    provider = EDINETRefresh(
        "test-key", sqlite_path=path, session=FakeSession(response(b"not a ZIP"))
    )
    with pytest.raises(EDINETProviderError, match="not a zip"):
        getattr(provider, method)("S100TEST")
    assert cache.read_bytes() == old_content
    assert not path.exists()


def test_cache_identity_does_not_claim_every_download_is_a_new_filing(tmp_path):
    db = tmp_path / "market.sqlite"
    first, later = _zip(b"first"), _zip(b"later")
    assert _retain_stored_archive(db, "S100TEST", 5, first, stored_on=date(2026, 1, 1)) == first
    assert _retain_stored_archive(db, "S100TEST", 5, later, stored_on=date(2026, 2, 1)) == first
    assert _retain_stored_archive(db, "S100TEST", 1, later, stored_on=date(2026, 2, 1)) == later
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute(
            "SELECT source_type, stored_on FROM edinet_source_archives ORDER BY source_type"
        ).fetchall() == [(1, "2026-02-01"), (5, "2026-01-01")]


def test_absent_source_reader_creates_neither_store_nor_session(tmp_path, monkeypatch):
    path = tmp_path / "absent.sqlite"

    def forbidden_session():
        pytest.fail("offline Reader constructed an HTTP session")

    monkeypatch.setattr("requests.Session", forbidden_session)
    with pytest.raises(EDINETProviderError, match="archive is unavailable"):
        EDINETReader(path).read_csv_zip("S100TEST")
    assert not path.exists()


def test_retention_rolls_back_insert_when_readback_fails(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as connection:
        with (
            patch(
                "baibai_engine.data_collection.edinet.archives.read_source_archive",
                side_effect=RuntimeError("readback failed"),
            ),
            pytest.raises(RuntimeError, match="readback failed"),
        ):
            retain_source_archive(
                connection, "S100TEST", 5, _zip(b"source"), stored_on=date(2026, 1, 1)
            )
        assert not connection.in_transaction
        assert read_source_archive(connection, "S100TEST", 5) is None


def test_retention_does_not_commit_a_callers_pending_transaction(tmp_path):
    path = tmp_path / "market.sqlite"
    with closing(open_connection(path)) as connection:
        connection.execute(
            "INSERT INTO edinet_source_archives "
            "(doc_id, source_type, stored_on, zip_content) VALUES (?, ?, ?, ?)",
            ("S100PENDING", 5, "2026-01-01", _zip(b"pending")),
        )
        with pytest.raises(ValueError, match="idle connection"):
            retain_source_archive(
                connection, "S100TEST", 5, _zip(b"new"), stored_on=date(2026, 1, 1)
            )
        assert connection.in_transaction
        assert read_source_archive(connection, "S100PENDING", 5) == _zip(b"pending")
        with closing(connect_read_only(path)) as other:
            assert read_source_archive(other, "S100PENDING", 5) is None
        connection.rollback()
        assert read_source_archive(connection, "S100PENDING", 5) is None


def test_binary_source_archives_survive_lake_export_and_hydration(tmp_path, monkeypatch):
    name = "edinet.source_archives"
    narrow_release_policy(monkeypatch, datasets=(name,), carries_history=None)
    db = tmp_path / "source.sqlite"
    originals = {1: _zip(b"<xbrl>\x00\xff</xbrl>"), 5: _zip("円\t－\t".encode("utf-16-le"))}
    for source_type, content in originals.items():
        _retain_stored_archive(db, "S100TEST", source_type, content, stored_on=date(2026, 1, 1))
    mirror = tmp_path / "mirror"
    stamp = datetime(2026, 2, 1, tzinfo=UTC)
    with sealed_sqlite_snapshot(
        sqlite_path=db, mirror_root=mirror, snapshot_id="sources"
    ) as snapshot:
        built = export_dataset_snapshot(
            dataset_name=name,
            mirror_root=mirror,
            source_snapshot=snapshot,
            producer_git_commit="a" * 40,
            build_id="source-archives",
            created_at=stamp,
        )
    manifest = built.manifest_path
    for partition in built.manifest.partitions:
        for obj in partition.objects:
            with pq.ParquetFile(mirror / obj.key) as parquet:
                assert parquet.metadata.num_row_groups == 2
                assert parquet.metadata.row_group(0).column(3).statistics is None
    release_path, release = create_release(
        dataset_manifest_paths=[manifest],
        mirror_root=mirror,
        release_id="source-archives",
        created_at=stamp,
    )
    source = LocalMirrorSource(mirror)
    fixed = resolve_release(
        source,
        release.release_id,
        manifest_sha256=hashlib.sha256(release_path.read_bytes()).hexdigest(),
    )
    with lake_session() as session:
        batches = list(
            iter_partition_rows(
                session,
                dataset=EDINET_SOURCE_ARCHIVES,
                paths=tuple(
                    mirror / obj.key for part in built.manifest.partitions for obj in part.objects
                ),
            )
        )
    assert [len(batch) for batch in batches] == [1, 1]
    target = tmp_path / "target.sqlite"
    open_connection(target).close()
    with lake_session() as session:
        report = hydrate_market_store(
            session,
            release=fixed,
            cache=LakeObjectCache(root=mirror, source=source),
            store=target,
            dataset_names=(name,),
        )
    assert report.rows == {name: 2}
    for source_type, content in originals.items():
        assert _read_stored_archive(target, "S100TEST", source_type) == content


def test_existing_broken_store_is_not_treated_as_a_network_cache_miss(tmp_path):
    path = tmp_path / "market.sqlite"
    path.write_bytes(b"not a SQLite database")
    provider = EDINETRefresh(
        "test-key",
        session=ExplodingSession(),
        sqlite_path=path,
    )
    with pytest.raises(sqlite3.DatabaseError):
        provider.download_csv_zip("S100TEST")
    assert path.read_bytes() == b"not a SQLite database"


def test_non_zip_response_cannot_replace_retained_inputs(tmp_path):
    path = tmp_path / "market.sqlite"
    original = _zip(b"retained filing")
    _retain_stored_archive(path, "S100KEPT", 5, original, stored_on=date(2026, 1, 1))
    provider = EDINETRefresh(
        "test-key",
        session=FakeSession(response(b"not a ZIP")),
        sqlite_path=path,
    )
    with pytest.raises(EDINETProviderError, match="not a zip"):
        provider.download_csv_zip("S100BAD")
    assert _read_stored_archive(path, "S100KEPT", 5) == original
    assert _read_stored_archive(path, "S100BAD", 5) is None


def test_native_listing_response_survives_lake_roundtrip(tmp_path, monkeypatch):
    name = "edinet.document_lists"
    # This two-day synthetic fixture checks native JSON/NULL roundtrip.
    narrow_release_policy(monkeypatch, datasets=(name,))
    db = tmp_path / "source.sqlite"
    original = json.dumps(
        {
            "metadata": {"resultset": {"count": 0}},
            "results": [],
            "unselected": {"zero": 0, "unknown": None, "text": "日本語"},
        },
        ensure_ascii=False,
    )
    store_edinet_documents(db, date(2026, 1, 1), [], response_json=original)
    store_edinet_documents(db, date(2026, 1, 2), [])
    mirror = tmp_path / "mirror"
    stamp = datetime(2026, 2, 1, tzinfo=UTC)
    with sealed_sqlite_snapshot(
        sqlite_path=db, mirror_root=mirror, snapshot_id="listings-source"
    ) as snapshot:
        built = export_dataset_snapshot(
            dataset_name=name,
            mirror_root=mirror,
            source_snapshot=snapshot,
            producer_git_commit="a" * 40,
            build_id="listings",
            created_at=stamp,
        )
    release_path, release = create_release(
        dataset_manifest_paths=[built.manifest_path],
        mirror_root=mirror,
        release_id="listings",
        created_at=stamp,
    )
    source = LocalMirrorSource(mirror)
    fixed = resolve_release(
        source,
        release.release_id,
        manifest_sha256=hashlib.sha256(release_path.read_bytes()).hexdigest(),
    )
    target = tmp_path / "target.sqlite"
    open_connection(target).close()
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=fixed,
            cache=LakeObjectCache(root=mirror, source=source),
            store=target,
            dataset_names=(name,),
        )
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute(
            "SELECT doc_date, response_json FROM edinet_document_lists ORDER BY doc_date"
        ).fetchall() == [("2026-01-01", original), ("2026-01-02", None)]
