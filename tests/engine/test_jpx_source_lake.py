from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store

from baibai_engine.data_collection.providers.jpx.models import JPXResponse
from baibai_engine.data_collection.providers.jpx.receipts import (
    complete_bundle,
    create_bundle,
    declare_bundle_resources,
    read_bundle,
    read_complete_bundle,
    retain_bundle_response,
)
from baibai_engine.storage.snapshots.datasets import LAKE_DATASETS
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import hydrate_market_store
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.sources import sha256_file
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.lake_origin import LakeStoreOrigin, write_lake_store_origin
from baibai_engine.storage.sqlite.source_cache.schema import open_connection

INDEX = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
WORKBOOK = "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/kessan.xlsx"
DATASETS = ("jquants.daily_bars", "jpx.source_bundles", "jpx.source_responses")


def _raw_rows(path: Path, table: str) -> tuple[tuple[object, ...], ...]:
    with closing(sqlite3.connect(path)) as conn:
        return tuple(conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall())  # nosec B608


def test_jpx_complete_and_incomplete_bundles_roundtrip_fixed_lake_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch, datasets=DATASETS, carries_history=None)
    source_path = market_store(tmp_path / "source.sqlite")
    started = datetime(2026, 10, 14, 23, 59, tzinfo=UTC)
    received = datetime(2026, 10, 15, 0, 1, tzinfo=UTC)
    raw_body = b"\x00\xffinvalid-xls-but-successful-http"
    conn = open_connection(source_path)
    try:
        complete_id = create_bundle(
            conn,
            "earnings_calendar",
            date(2026, 10, 15),
            started,
            (("index", INDEX), ("workbook", WORKBOOK)),
        )
        retain_bundle_response(
            conn,
            complete_id,
            "index",
            JPXResponse(INDEX, received, "text/html; charset=UTF-8", b"<bad>"),
        )
        retain_bundle_response(
            conn,
            complete_id,
            "workbook",
            JPXResponse(WORKBOOK, received, "application/vnd.ms-excel", raw_body),
        )
        complete_bundle(conn, complete_id)
        incomplete_id = create_bundle(conn, "delistings", date(2026, 10, 15), started)
        retain_bundle_response(
            conn, incomplete_id, "index", JPXResponse(INDEX, received, "text/html", b"<index>")
        )
        declare_bundle_resources(conn, incomplete_id, (("index", INDEX), ("workbook", WORKBOOK)))
    finally:
        conn.close()

    expected = {
        name: _raw_rows(source_path, name)
        for name in ("jpx_source_bundles", "jpx_source_responses")
    }
    mirror = tmp_path / "mirror"
    built = datetime(2026, 10, 16, tzinfo=UTC)
    with sealed_sqlite_snapshot(sqlite_path=source_path, mirror_root=mirror) as snapshot:
        manifests = [
            export_dataset_snapshot(
                dataset_name=name,
                mirror_root=mirror,
                source_snapshot=snapshot,
                producer_git_commit="a" * 40,
                build_id=f"jpx-raw-{name.replace('.', '-')}",
                created_at=built,
            ).manifest_path
            for name in DATASETS
        ]
    for name in DATASETS[1:]:
        dataset = LAKE_DATASETS[name]
        assert dataset.layer == "l1_source"
        assert dataset.contract_version == 1
        assert dataset.coverage_authority == "unproven"
        assert dataset.population_column is None
        assert dataset.date_column == ("started_on" if name.endswith("bundles") else "received_on")
    manifest_path, manifest = create_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="jpx-raw-fixed",
        created_at=built,
    )
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source, manifest.release_id, manifest_sha256=sha256_file(manifest_path)
    )
    destination = tmp_path / "hydrated.sqlite"
    shutil.copyfile(source_path, destination)
    with closing(open_connection(destination)) as conn:
        conn.execute("DELETE FROM jpx_source_responses")
        conn.execute("DELETE FROM jpx_source_bundles")
        write_lake_store_origin(conn, LakeStoreOrigin(release.release_id, release.manifest_sha256))
        conn.commit()
    cache = LakeObjectCache(root=tmp_path / "cache", source=source)
    with lake_session() as session:
        report = hydrate_market_store(
            session, release=release, cache=cache, store=destination, dataset_names=DATASETS
        )
    assert report.rows["jpx.source_bundles"] == 2
    assert report.rows["jpx.source_responses"] == 3
    for table, rows in expected.items():
        assert _raw_rows(destination, table) == rows
    with closing(open_connection(destination)) as conn:
        complete = read_complete_bundle(conn, complete_id)
        incomplete = read_bundle(conn, incomplete_id)
    assert complete.responses[1].response.body == raw_body
    assert complete.expected_resources == (("index", INDEX), ("workbook", WORKBOOK))
    assert incomplete.completed_at is None
    assert incomplete.expected_resources == (("index", INDEX), ("workbook", WORKBOOK))
