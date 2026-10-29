from __future__ import annotations

import hashlib
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.tradingview import DAY, NOW, seed_snapshot, store, stored

from baibai_engine.data_collection.providers.tradingview.receipts import retain_tool_result
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.hydrate import dehydrate_market_store, hydrate_market_store
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import (
    accepted_dataset,
    materialize_partitions,
    resolve_release,
    selected_partitions,
)
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import (
    export_dataset_snapshot,
    sealed_sqlite_snapshot,
    validate_snapshot_parity,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile

NAME = "tradingview.forecast_snapshots"


def test_raw_only_failed_acquisition_receipt_survives_fixed_release(tmp_path, monkeypatch):
    dataset = "tradingview.source_receipts"
    narrow_release_policy(monkeypatch, datasets=(dataset,))
    source_path = store(tmp_path / "source.sqlite", 1)
    results = (
        CallToolResult(isError=True, content=[], structuredContent={"status": 429}),
        CallToolResult(isError=True, content=[], structuredContent={"status": 503}),
        CallToolResult(isError=False, content=[], structuredContent={"unexpected": "malformed"}),
    )
    with closing(sqlite3.connect(source_path)) as conn:
        for result in results:
            retain_tool_result(
                conn,
                snapshot_date=DAY,
                master_asof=DAY,
                tool_name="test_tool",
                received_at_utc=NOW,
                requested_symbols=["1000"],
                requested_columns=["close"],
                result=result,
            )
    with closing(sqlite3.connect(source_path)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tradingview_source_receipts WHERE snapshot_date=?",
            (DAY.isoformat(),),
        ).fetchone() == (3,)
    assert stored(source_path) == []
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=source_path, mirror_root=mirror) as snapshot:
        manifest = export_dataset_snapshot(
            dataset=LAKE_DATASETS[dataset],
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            source_snapshot=snapshot,
            build_id="raw-only-receipts",
        ).manifest_path
    release_path, _ = create_release(
        dataset_manifest_paths=[manifest],
        mirror_root=mirror,
        release_id="raw-only-release",
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    digest = hashlib.sha256(release_path.read_bytes()).hexdigest()
    source = LocalMirrorSource(mirror)
    release = resolve_release(source, release_id="raw-only-release", manifest_sha256=digest)
    cache = LakeObjectCache(root=mirror, source=source)
    restored = tmp_path / "restored.sqlite"
    open_connection(restored).close()
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=release,
            cache=cache,
            store=restored,
            dataset_names=(dataset,),
            catalog=LAKE_DATASETS,
        )
    with closing(sqlite3.connect(restored)) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM tradingview_source_receipts WHERE snapshot_date=?",
            (DAY.isoformat(),),
        ).fetchone() == (3,)
    assert stored(restored) == []
    with closing(sqlite3.connect(restored)) as conn:
        restored_json = [
            row[0]
            for row in conn.execute(
                "SELECT result_json FROM tradingview_source_receipts ORDER BY result_json"
            )
        ]
    with closing(sqlite3.connect(source_path)) as conn:
        original_json = [
            row[0]
            for row in conn.execute(
                "SELECT result_json FROM tradingview_source_receipts ORDER BY result_json"
            )
        ]
    assert restored_json == original_json


def test_month_partition_fixed_query_and_hydrate_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch, datasets=[NAME])
    path = store(tmp_path / "market.sqlite", 2)

    seed_snapshot(path, ["TSE:1000", "TSE:1001"], missing=("TSE:1001",))
    next_day = date(2026, 10, 1)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE jquants_master_snapshots SET snapshot_date=?, "
            "record_json=json_set(record_json, '$.Date', ?)",
            (next_day.isoformat(), next_day.isoformat()),
        )
        conn.execute("UPDATE jquants_market_calendar SET day=?", (next_day.isoformat(),))
    seed_snapshot(
        path,
        ["TSE:1000", "TSE:1001"],
        day=next_day,
        master=next_day,
        received=datetime(2026, 10, 1, 7, tzinfo=UTC),
        missing=("TSE:1001",),
    )
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=mirror) as snapshot:
        exported = export_dataset_snapshot(
            dataset=LAKE_DATASETS[NAME],
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            source_snapshot=snapshot,
            build_id="tv-build",
        )
    assert [p.values for p in exported.manifest.partitions] == [
        {"year": 2026, "month": 9},
        {"year": 2026, "month": 10},
    ]
    assert exported.manifest.totals.rows == 4
    validate_snapshot_parity(
        sqlite_path=path,
        mirror_root=mirror,
        manifest=exported.manifest,
        dataset=LAKE_DATASETS[exported.manifest.dataset],
    )
    manifest_path, _ = create_release(
        dataset_manifest_paths=[exported.manifest_path],
        mirror_root=mirror,
        release_id="tv-release",
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    source = LocalMirrorSource(mirror)
    release = resolve_release(source, release_id="tv-release", manifest_sha256=digest)
    cache = LakeObjectCache(root=mirror, source=source)
    paths = materialize_partitions(
        cache,
        dataset=accepted_dataset(release, LAKE_DATASETS[NAME]),
        partitions=selected_partitions(release, NAME, periods=[(2026, 9), (2026, 10)]),
    )
    with lake_session() as session:
        rows = session.connection.execute(
            "SELECT snapshot_date,ticker,fetch_status,eps_forecast_next_fy "
            "FROM read_parquet($files) WHERE snapshot_date BETWEEN $start AND $end "
            "ORDER BY snapshot_date,ticker",
            {"files": [str(path) for path in paths], "start": DAY.isoformat(), "end": "2026-10-01"},
        ).fetchall()
    assert rows == [
        (DAY.isoformat(), "1000", "ok", -5.0),
        (DAY.isoformat(), "1001", "unresolved", None),
        (next_day.isoformat(), "1000", "ok", -5.0),
        (next_day.isoformat(), "1001", "unresolved", None),
    ]
    hydrated = tmp_path / "hydrated.sqlite"
    open_connection(hydrated).close()
    with lake_session() as session:
        report = hydrate_market_store(
            session,
            release=release,
            cache=cache,
            store=hydrated,
            dataset_names=[NAME],
            catalog=LAKE_DATASETS,
        )
    assert report.rows[NAME] == 4
    assert stored(hydrated) == stored(path)
    dehydrate_market_store(hydrated, release=release, catalog=LAKE_DATASETS)
    assert stored(hydrated) == []
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=release,
            cache=cache,
            store=hydrated,
            dataset_names=[NAME],
            catalog=LAKE_DATASETS,
        )
    assert stored(hydrated) == stored(path)
