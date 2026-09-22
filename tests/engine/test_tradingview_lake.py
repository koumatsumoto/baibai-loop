from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.engine.test_tradingview_collector import DAY, NOW, payload, store, stored
from tests.helpers.lake_policy import narrow_release_policy
from tools.l1_mcp.contract import Query
from tools.l1_mcp.query import execute_query

from baibai_engine.market.lake.duck import lake_session
from baibai_engine.market.lake.hydrate import dehydrate_market_store, hydrate_market_store
from baibai_engine.market.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.market.lake.reader import resolve_release
from baibai_engine.market.lake.release import create_l1_release
from baibai_engine.market.lake.writer import (
    export_legacy_sqlite,
    sealed_sqlite_snapshot,
    validate_legacy_parity,
)
from baibai_engine.market.sqlite import open_connection
from baibai_engine.market.tradingview.collector import collect

NAME = "tradingview.forecast_snapshots"


def test_month_partition_fixed_query_and_hydrate_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch, datasets=[NAME])
    path = store(tmp_path / "market.sqlite", 2)

    async def fetch(symbols):
        return payload(symbols, missing=symbols[1:])

    asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW))
    next_day = date(2026, 10, 1)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE jquants_master_snapshots SET snapshot_date=?", (next_day.isoformat(),))
        conn.execute("UPDATE jquants_market_calendar SET day=?", (next_day.isoformat(),))
    asyncio.run(collect(path, next_day, fetch, clock=lambda: datetime(2026, 10, 1, 7, tzinfo=UTC)))
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=mirror) as snapshot:
        exported = export_legacy_sqlite(
            dataset_name=NAME,
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
    validate_legacy_parity(sqlite_path=path, mirror_root=mirror, manifest=exported.manifest)
    manifest_path, _ = create_l1_release(
        dataset_manifest_paths=[exported.manifest_path],
        mirror_root=mirror,
        release_id="tv-release",
    )
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    source = LocalMirrorSource(mirror)
    release = resolve_release(source, release_id="tv-release", manifest_sha256=digest)
    cache = LakeObjectCache(root=mirror, source=source)
    request = Query.model_validate(
        {
            "release_ref": {"release_id": "tv-release", "manifest_sha256": digest},
            "sources": [
                {"dataset": NAME, "alias": "tv", "from": DAY.isoformat(), "to": "2026-10-01"}
            ],
            "sql": "SELECT snapshot_date,ticker,fetch_status,eps_forecast_next_fy FROM tv ORDER BY snapshot_date,ticker",
            "max_rows": 4,
        }
    )
    query = execute_query(release, request, cache)
    assert query["rows"] == [
        ["2026-09-18", "1000", "ok", -5.0],
        ["2026-09-18", "1001", "unresolved", None],
        ["2026-10-01", "1000", "ok", -5.0],
        ["2026-10-01", "1001", "unresolved", None],
    ]
    hydrated = tmp_path / "hydrated.sqlite"
    open_connection(hydrated).close()
    with lake_session() as session:
        report = hydrate_market_store(
            session, release=release, cache=cache, store=hydrated, dataset_names=[NAME]
        )
    assert report.rows[NAME] == 4
    assert stored(hydrated) == stored(path)
    dehydrate_market_store(hydrated, release=release)
    assert stored(hydrated) == []
    with lake_session() as session:
        hydrate_market_store(
            session, release=release, cache=cache, store=hydrated, dataset_names=[NAME]
        )
    assert stored(hydrated) == stored(path)
