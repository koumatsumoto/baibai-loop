from __future__ import annotations

import asyncio
import hashlib
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from tests.engine.test_tradingview_collector import DAY, NOW, _sdk, payload, store, stored
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.screening_sqlite import make_master_records

from baibai_engine.collection.tradingview.receipts import retain_tool_result
from baibai_engine.external_assessment.tradingview.collector import collect, preflight_snapshot
from baibai_engine.storage.lake.duck import lake_session
from baibai_engine.storage.lake.hydrate import dehydrate_market_store, hydrate_market_store
from baibai_engine.storage.lake.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.lake.reader import (
    accepted_dataset,
    materialize_partitions,
    resolve_release,
    selected_partitions,
)
from baibai_engine.storage.lake.release import create_release
from baibai_engine.storage.lake.writer import (
    export_dataset_snapshot,
    sealed_sqlite_snapshot,
    validate_snapshot_parity,
)
from baibai_engine.storage.observations.coverage import record_range_source_coverage
from baibai_engine.storage.observations.ingest.jquants import store_jquants_master
from baibai_engine.storage.observations.jquants import store_jquants_market_calendar
from baibai_engine.storage.observations.lake_origin import LakeStoreOrigin, write_lake_store_origin
from baibai_engine.storage.observations.reader import read_eq_master_exact
from baibai_engine.storage.observations.schema import open_connection

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
            dataset_name=dataset,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            source_snapshot=snapshot,
            build_id="raw-only-receipts",
        ).manifest_path
    release_path, _ = create_release(
        dataset_manifest_paths=[manifest], mirror_root=mirror, release_id="raw-only-release"
    )
    digest = hashlib.sha256(release_path.read_bytes()).hexdigest()
    source = LocalMirrorSource(mirror)
    release = resolve_release(source, release_id="raw-only-release", manifest_sha256=digest)
    cache = LakeObjectCache(root=mirror, source=source)
    restored = tmp_path / "restored.sqlite"
    open_connection(restored).close()
    with lake_session() as session:
        hydrate_market_store(
            session, release=release, cache=cache, store=restored, dataset_names=(dataset,)
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


@pytest.mark.parametrize("manual_observation", [False, True])
def test_l1_only_publish_restores_snapshot_without_market_push(
    tmp_path, monkeypatch, manual_observation
):
    datasets = (
        "jquants.daily_bars",
        "jquants.market_calendar",
        "jquants.master_snapshots",
        "tradingview.source_receipts",
        NAME,
    )
    narrow_release_policy(monkeypatch, datasets=datasets)
    today = date(2026, 9, 20) if manual_observation else date(2026, 9, 21)
    observed = datetime(
        today.year, today.month, today.day, 1 if manual_observation else 7, tzinfo=UTC
    )

    async def no_wait(_seconds):
        pass

    options = {"master_asof": DAY, "sleep": no_wait} if manual_observation else {}
    interval = 15 if manual_observation else 0
    path = tmp_path / "working.sqlite"
    open_connection(path).close()
    store_jquants_master(path, make_master_records(DAY), requested_asof=DAY)
    store_jquants_market_calendar(
        path,
        [
            {"Date": "2026-09-18", "HolDiv": "1"},
            {"Date": "2026-09-19", "HolDiv": "0"},
            {"Date": "2026-09-20", "HolDiv": "0"},
            {"Date": "2026-09-21", "HolDiv": "1"},
        ],
        requested_start=DAY,
        requested_end=date(2026, 9, 21),
    )
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close) VALUES (?,?,?)",
            ("1000", DAY.isoformat(), 100),
        )
        record_range_source_coverage(
            conn,
            source="jquants_daily_bars",
            operation="get_daily_quotes",
            table="jquants_daily_bars",
            date_column="traded_at",
            requested_start=DAY,
            requested_end=DAY,
            status="ok",
            error=None,
        )

    async def fetch(symbols, _columns):
        return _sdk(payload(symbols))

    asyncio.run(collect(path, DAY, fetch, interval=0, clock=lambda: NOW))
    mirror = tmp_path / "mirror"

    def publish(release_id, source_path=path):
        with sealed_sqlite_snapshot(sqlite_path=source_path, mirror_root=mirror) as snapshot:
            manifests = [
                export_dataset_snapshot(
                    dataset_name=name,
                    mirror_root=mirror,
                    producer_git_commit="a" * 40,
                    source_snapshot=snapshot,
                    build_id=f"{release_id}-{i}",
                ).manifest_path
                for i, name in enumerate(datasets)
            ]
        manifest_path, _ = create_release(
            dataset_manifest_paths=manifests,
            mirror_root=mirror,
            release_id=release_id,
        )
        digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        source = LocalMirrorSource(mirror)
        return resolve_release(
            source, release_id=release_id, manifest_sha256=digest
        ), LakeObjectCache(root=mirror, source=source)

    prior, prior_cache = publish("prior-tv-release")
    conn = open_connection(path)
    write_lake_store_origin(conn, LakeStoreOrigin(prior.release_id, prior.manifest_sha256))
    conn.commit()
    conn.close()
    dehydrate_market_store(path, release=prior)
    remote = tmp_path / "remote-market.sqlite"
    shutil.copy2(path, remote)
    with lake_session() as session:
        hydrate_market_store(
            session, release=prior, cache=prior_cache, store=path, dataset_names=datasets
        )
    if not manual_observation:
        store_jquants_master(path, make_master_records(today), requested_asof=today)
    from baibai_engine.external_assessment.tradingview.observations import ProviderHTTPError

    calls = 0

    async def partial_fetch(symbols, _columns):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ProviderHTTPError(429)
        return _sdk(payload(symbols))

    first = asyncio.run(
        collect(
            path,
            today,
            partial_fetch,
            interval=interval,
            clock=lambda: observed,
            **options,
        )
    )
    assert (first["status"], first["rows_added"], first["remaining"]) == ("partial", 50, 2450)
    first_rows = stored(path)
    current, current_cache = publish("current-tv-release")
    assert current.data_as_of == DAY

    # The remote market object remains the prior dehydrated copy: only L1 advanced.
    with closing(sqlite3.connect(remote)) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM tradingview_forecast_snapshots").fetchone()[0] == 0
        )
    next_slot = tmp_path / "next-slot.sqlite"
    shutil.copy2(remote, next_slot)
    with lake_session() as session:
        hydrate_market_store(
            session, release=current, cache=current_cache, store=next_slot, dataset_names=datasets
        )
    state = preflight_snapshot(next_slot, today, master_asof=DAY if manual_observation else None)
    assert (state["status"], state["needs_master"], state["rows"], state["remaining"]) == (
        "needs_fetch",
        False,
        50,
        2450,
    )
    assert read_eq_master_exact(next_slot, today) is None
    resumed = asyncio.run(
        collect(
            next_slot,
            today,
            fetch,
            interval=interval,
            clock=lambda: observed,
            **options,
        )
    )
    assert (resumed["status"], resumed["rows_added"], resumed["remaining"]) == ("saved", 2450, 0)
    first_today = [row for row in first_rows if row[0] == today.isoformat()]
    resumed_today = [row for row in stored(next_slot) if row[0] == today.isoformat()]
    assert resumed_today[:50] == first_today
    assert (
        preflight_snapshot(next_slot, today, master_asof=DAY if manual_observation else None)[
            "status"
        ]
        == "already_saved"
    )
    completed, completed_cache = publish("completed-tv-release", next_slot)
    fixed_readback = tmp_path / "completed-readback.sqlite"
    shutil.copy2(remote, fixed_readback)
    with lake_session() as session:
        hydrate_market_store(
            session,
            release=completed,
            cache=completed_cache,
            store=fixed_readback,
            dataset_names=datasets,
        )
    assert stored(fixed_readback) == stored(next_slot)
    for release, cache, expected in (
        (current, current_cache, 50),
        (completed, completed_cache, 2500),
    ):
        paths = materialize_partitions(
            cache,
            dataset=accepted_dataset(release, NAME),
            partitions=selected_partitions(release, NAME, periods=[(today.year, today.month)]),
        )
        with lake_session() as session:
            result = session.connection.execute(
                "SELECT COUNT(*) FROM read_parquet($files) WHERE snapshot_date = $day",
                {"files": [str(path) for path in paths], "day": today.isoformat()},
            ).fetchone()
        assert result == (expected,)


def test_month_partition_fixed_query_and_hydrate_roundtrip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    narrow_release_policy(monkeypatch, datasets=[NAME])
    path = store(tmp_path / "market.sqlite", 2)

    async def fetch(symbols, _columns):
        return _sdk(payload(symbols, missing=symbols[1:]))

    asyncio.run(collect(path, DAY, fetch, clock=lambda: NOW))
    next_day = date(2026, 10, 1)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "UPDATE jquants_master_snapshots SET snapshot_date=?, "
            "record_json=json_set(record_json, '$.Date', ?)",
            (next_day.isoformat(), next_day.isoformat()),
        )
        conn.execute("UPDATE jquants_market_calendar SET day=?", (next_day.isoformat(),))
    asyncio.run(collect(path, next_day, fetch, clock=lambda: datetime(2026, 10, 1, 7, tzinfo=UTC)))
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=path, mirror_root=mirror) as snapshot:
        exported = export_dataset_snapshot(
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
    validate_snapshot_parity(sqlite_path=path, mirror_root=mirror, manifest=exported.manifest)
    manifest_path, _ = create_release(
        dataset_manifest_paths=[exported.manifest_path],
        mirror_root=mirror,
        release_id="tv-release",
    )
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    source = LocalMirrorSource(mirror)
    release = resolve_release(source, release_id="tv-release", manifest_sha256=digest)
    cache = LakeObjectCache(root=mirror, source=source)
    paths = materialize_partitions(
        cache,
        dataset=accepted_dataset(release, NAME),
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
