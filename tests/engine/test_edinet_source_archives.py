from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import UTC, date, datetime
from io import BytesIO

import pyarrow.parquet as pq
import pytest
from tests.helpers.http_doubles import ExplodingSession, FakeSession, response
from tests.helpers.lake_policy import narrow_release_policy

from baibai_engine.market.edinet_models import EDINETProviderError
from baibai_engine.market.edinet_sources import read_source_archive, retain_source_archive
from baibai_engine.market.providers.edinet import EDINETProvider
from baibai_engine.storage.lake.datasets import EDINET_SOURCE_ARCHIVES
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import hydrate_market_store
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import iter_partition_rows, resolve_release
from baibai_engine.storage.lake.release import create_release
from baibai_engine.storage.lake.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.observations.ingest.edinet import store_edinet_documents
from baibai_engine.storage.observations.schema import open_connection


def _zip(content: bytes) -> bytes:
    target = BytesIO()
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("source/document", content)
    return target.getvalue()


@pytest.mark.parametrize(
    ("source_type", "method"), [(1, "download_xbrl_zip"), (5, "download_csv_zip")]
)
def test_downloaded_bytes_are_available_without_the_local_cache(tmp_path, source_type, method):
    content = _zip(b"original units, missing facts, and unselected fields")
    reply = response(content)
    provider = EDINETProvider(
        "test-key",
        tmp_path / "first-cache",
        session=FakeSession(reply),
        sqlite_path=tmp_path / "market.sqlite",
    )
    assert getattr(provider, method)("S100TEST") == content
    assert reply.close_count == 1
    assert read_source_archive(tmp_path / "market.sqlite", "S100TEST", source_type) == content
    offline = EDINETProvider(
        None,
        tmp_path / "different-cache",
        session=ExplodingSession(),
        sqlite_path=tmp_path / "market.sqlite",
        cache_only=True,
    )
    assert getattr(offline, method)("S100TEST") == content
    assert not (tmp_path / "different-cache").exists()


@pytest.mark.parametrize(
    ("kind", "source_type", "method"),
    [
        ("csv", 5, "download_csv_zip"),
        ("xbrl", 1, "download_xbrl_zip"),
    ],
)
def test_existing_zip_is_retained_without_repeating_acquisition(
    tmp_path, kind, source_type, method
):
    content = _zip(b"already downloaded")
    cache = tmp_path / "cache" / "edinet" / f"{kind}_zips" / "S100TEST.zip"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(content)
    provider = EDINETProvider(
        None,
        tmp_path / "cache",
        session=ExplodingSession(),
        sqlite_path=tmp_path / "market.sqlite",
        cache_only=True,
    )
    assert getattr(provider, method)("S100TEST") == content
    assert read_source_archive(tmp_path / "market.sqlite", "S100TEST", source_type) == content
    assert cache.read_bytes() == content


def test_cache_identity_does_not_claim_every_download_is_a_new_filing(tmp_path):
    db = tmp_path / "market.sqlite"
    first, later = _zip(b"first"), _zip(b"later")
    assert retain_source_archive(db, "S100TEST", 5, first, stored_on=date(2026, 1, 1)) == first
    assert retain_source_archive(db, "S100TEST", 5, later, stored_on=date(2026, 2, 1)) == first
    assert retain_source_archive(db, "S100TEST", 1, later, stored_on=date(2026, 2, 1)) == later
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute(
            "SELECT source_type, stored_on FROM edinet_source_archives ORDER BY source_type"
        ).fetchall() == [(1, "2026-02-01"), (5, "2026-01-01")]


def test_absent_source_read_does_not_create_a_store(tmp_path):
    path = tmp_path / "absent.sqlite"
    assert read_source_archive(path, "S100TEST", 5) is None
    assert not path.exists()


def test_binary_source_archives_survive_lake_export_and_hydration(tmp_path, monkeypatch):
    name = "edinet.source_archives"
    narrow_release_policy(monkeypatch, datasets=(name,), carries_history=None)
    db = tmp_path / "source.sqlite"
    originals = {1: _zip(b"<xbrl>\x00\xff</xbrl>"), 5: _zip("円\t－\t".encode("utf-16-le"))}
    for source_type, content in originals.items():
        retain_source_archive(db, "S100TEST", source_type, content, stored_on=date(2026, 1, 1))
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
        assert read_source_archive(target, "S100TEST", source_type) == content


def test_existing_broken_store_is_not_treated_as_a_network_cache_miss(tmp_path):
    path = tmp_path / "market.sqlite"
    path.write_bytes(b"not a SQLite database")
    provider = EDINETProvider(
        "test-key",
        tmp_path / "cache",
        session=ExplodingSession(),
        sqlite_path=path,
    )
    with pytest.raises(sqlite3.DatabaseError):
        provider.download_csv_zip("S100TEST")
    assert path.read_bytes() == b"not a SQLite database"


def test_non_zip_response_cannot_replace_retained_inputs(tmp_path):
    path = tmp_path / "market.sqlite"
    original = _zip(b"retained filing")
    retain_source_archive(path, "S100KEPT", 5, original, stored_on=date(2026, 1, 1))
    provider = EDINETProvider(
        "test-key",
        tmp_path / "cache",
        session=FakeSession(response(b"not a ZIP")),
        sqlite_path=path,
    )
    with pytest.raises(EDINETProviderError, match="not a zip"):
        provider.download_csv_zip("S100BAD")
    assert read_source_archive(path, "S100KEPT", 5) == original
    assert read_source_archive(path, "S100BAD", 5) is None


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
