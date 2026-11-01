"""外部評価の固定universeと保存原本readbackを使う合成入力。"""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime

from baibai_engine.storage.snapshots.keys import current_release_pointer_key
from baibai_engine.storage.snapshots.models import canonical_lake_model_bytes
from baibai_engine.storage.snapshots.objects import LocalMirrorSource
from baibai_engine.storage.snapshots.reader import resolve_current_release
from baibai_engine.storage.snapshots.release import LakeReleasePointer, create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.coverage import record_range_source_coverage
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    write_lake_store_origin,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.master import TSE_33_SECTORS, insert_master_rows
from tests.helpers.r2_store import MemoryR2Store

ASOF = date(2026, 10, 11)
RECEIVED = datetime(2026, 10, 11, 0, 0, 1, 123456, tzinfo=UTC)
ORIGIN = LakeStoreOrigin("synthetic-fixed-release", "a" * 64)


def seed_database(path, *, count=2500, snapshot=ASOF, origin=True):
    sectors = sorted(TSE_33_SECTORS)
    markets = ("PRIME", "standard", "グロース", "東証一部")
    tickers = tuple(str(1000 + index) for index in range(count))
    connection = open_connection(path)
    try:
        insert_master_rows(
            connection,
            [
                (
                    snapshot.isoformat(),
                    ticker,
                    f"合成会社{index}",
                    markets[index % 4],
                    sectors[index % 33],
                    "011",
                )
                for index, ticker in enumerate(tickers)
            ],
        )
        if origin:
            write_lake_store_origin(connection, ORIGIN)
        connection.execute(
            "INSERT INTO source_coverage(source,coverage_key,fetched_at_utc,record_count,status) VALUES (?,?,?,?,?)",
            ("synthetic", "saved", RECEIVED.isoformat(), count, "ok"),
        )
        connection.commit()
    finally:
        connection.close()
    return tickers


def build_master_lake(
    tmp_path, monkeypatch, *, snapshot=ASOF, count=2500, mutate=None, calendar=None
):
    """Real contracts and Parquet, plus unrelated data that must never be fetched."""
    datasets = ("jquants.master_snapshots", "jquants.daily_bars")
    if calendar is not None:
        datasets += ("jquants.market_calendar",)
    narrow_release_policy(monkeypatch, datasets=datasets)
    database = tmp_path / "market.sqlite"
    seed_database(database, snapshot=snapshot, count=count)
    with closing(sqlite3.connect(database)) as connection, connection:
        insert_master_rows(
            connection,
            [
                (snapshot.isoformat(), "130A", "合成英字会社", "PRIME", "電気機器", "011"),
                (snapshot.isoformat(), "9401", "合成ETF", "PRIME", "その他", "014"),
                (snapshot.isoformat(), "9402", "合成PRO", "TOKYO PRO MARKET", "電気機器", "011"),
                (snapshot.isoformat(), "9403", "合成市場外", "RESIDUAL", "電気機器", "011"),
                ("2025-12-31", "130A", "合成旧年会社", "PRIME", "電気機器", "011"),
            ],
        )
        connection.execute(
            "INSERT INTO jquants_daily_bars(ticker,traded_at,close,volume) VALUES (?,?,?,?)",
            ("130A", "2026-09-30", 1, 1),
        )
        record_range_source_coverage(
            connection,
            source="jquants_daily_bars",
            operation="get_daily_quotes",
            table="jquants_daily_bars",
            date_column="traded_at",
            requested_start=date(2026, 9, 30),
            requested_end=date(2026, 9, 30),
            status="ok",
            error=None,
        )
        if calendar is not None:
            connection.executemany("INSERT INTO jquants_market_calendar VALUES (?,?)", calendar)
            connection.execute(
                "INSERT INTO source_coverage(source,coverage_key,coverage_start,coverage_end,"
                "fetched_at_utc,record_count,status) VALUES (?,?,?,?,?,?,?)",
                (
                    "jquants_market_calendar",
                    "synthetic-range",
                    min(day for day, _ in calendar),
                    max(day for day, _ in calendar),
                    RECEIVED.isoformat(),
                    len(calendar),
                    "ok",
                ),
            )
        if mutate is not None:
            mutate(connection)
    mirror = tmp_path / "published"
    with sealed_sqlite_snapshot(sqlite_path=database, mirror_root=mirror) as snapshot_source:
        manifests = [
            export_dataset_snapshot(
                dataset=LAKE_DATASETS[name],
                mirror_root=mirror,
                producer_git_commit="a" * 40,
                source_snapshot=snapshot_source,
                build_id=f"synthetic-{name.replace('.', '-')}",
                created_at=RECEIVED,
            ).manifest_path
            for name in datasets
        ]
    path, release = create_release(
        dataset_manifest_paths=manifests,
        mirror_root=mirror,
        release_id="synthetic-master-only",
        created_at=RECEIVED,
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    pointer = LakeReleasePointer(
        release_id=release.release_id,
        manifest_key=path.relative_to(mirror).as_posix(),
        manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    pointer_path = mirror / current_release_pointer_key()
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_bytes(canonical_lake_model_bytes(pointer))
    fixed = resolve_current_release(
        LocalMirrorSource(mirror),
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    store = MemoryR2Store()
    for item in mirror.rglob("*"):
        if item.is_file():
            store.store_object(item.relative_to(mirror).as_posix(), item.read_bytes())
    return store, fixed, database
