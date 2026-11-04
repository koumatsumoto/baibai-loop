from __future__ import annotations

import gzip
import json
from contextlib import closing
from datetime import UTC, datetime

import duckdb
import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store

from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import materialize_partitions
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.l1_price_export import export_prices_from_l1
from baibai_jobs.publication.policy import release_policy_for_profile


def price_source(tmp_path, second):
    database = market_store(tmp_path / "source.sqlite")
    records = [
        ("20260131", {"data": [{"Code": "13010", "Date": "2026-01-31", "C": 100}]}),
        ("20260201", second),
    ]
    with closing(open_connection(database)) as conn, conn:
        for day, body in records:
            conn.execute(
                "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    day,
                    0,
                    "daily_bars",
                    json.dumps({"date": day}),
                    "2026-02-03T00:00:00+00:00",
                    "2026-02-03",
                    gzip.compress(json.dumps(body).encode(), mtime=0),
                ),
            )
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=database, mirror_root=mirror) as snapshot:
        source = export_dataset_snapshot(
            dataset=LAKE_DATASETS["jquants.source_pages"],
            mirror_root=mirror,
            source_snapshot=snapshot,
            producer_git_commit="a" * 40,
            build_id="source",
            created_at=datetime(2026, 2, 4, tzinfo=UTC),
        )
    database.unlink()
    return mirror, source


def test_price_export_from_l1_across_months(tmp_path, monkeypatch):
    narrow_release_policy(monkeypatch, datasets=("jquants.source_pages", "jquants.daily_bars"))
    mirror, source = price_source(
        tmp_path, {"data": [{"Code": "13010", "Date": "2026-02-01", "C": 110}]}
    )
    when = datetime(2026, 2, 4, tzinfo=UTC)
    result = export_prices_from_l1(
        source=source,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        known_at=when,
        created_at=when,
        build_id="price-derived",
    )
    assert result.source_days == 2
    assert result.missing_days == result.partial_days == 0
    assert result.build.manifest.coverage_status == "complete"
    create_release(
        dataset_manifest_paths=[source.manifest_path, result.build.manifest_path],
        mirror_root=mirror,
        release_id="price-release",
        created_at=when,
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    paths = materialize_partitions(
        LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
        dataset=LAKE_DATASETS["jquants.daily_bars"],
        partitions=result.build.manifest.partitions,
    )
    with duckdb.connect(":memory:") as conn:
        assert conn.execute(
            "SELECT ticker, traded_at, close FROM read_parquet(?) ORDER BY traded_at",
            [[str(p) for p in paths]],
        ).fetchall() == [("1301", "2026-01-31", 100.0), ("1301", "2026-02-01", 110.0)]


def test_incomplete_latest_price_day_stops_release(tmp_path, monkeypatch):
    narrow_release_policy(monkeypatch, datasets=("jquants.source_pages", "jquants.daily_bars"))
    mirror, source = price_source(tmp_path, {"data": [], "pagination_key": "next"})
    when = datetime(2026, 2, 4, tzinfo=UTC)
    result = export_prices_from_l1(
        source=source,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        known_at=when,
        created_at=when,
        build_id="price-partial",
    )
    assert result.missing_days == 1
    assert result.build.manifest.coverage_status == "partial"
    with pytest.raises(ValueError, match="coverage is partial"):
        create_release(
            dataset_manifest_paths=[source.manifest_path, result.build.manifest_path],
            mirror_root=mirror,
            release_id="price-partial-release",
            created_at=when,
            catalog=LAKE_DATASETS,
            policy=release_policy_for_profile("production"),
        )


def test_market_publication_ignores_old_price_projection(tmp_path, monkeypatch):
    from tests.helpers.r2_store import MemoryR2Store

    import baibai_jobs.publication.snapshot_publication as publication

    names = ("jquants.source_pages", "jquants.daily_bars")
    narrow_release_policy(monkeypatch, datasets=names)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {k: LAKE_DATASETS[k] for k in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    database = market_store(
        tmp_path / "market.sqlite", daily_bars=(("1301", "2026-01-05", 999.0, 1.0),)
    )
    raw = {"data": [{"Code": "13010", "Date": "2026-01-05", "C": 100}]}
    with closing(open_connection(database)) as conn, conn:
        conn.execute(
            "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "source",
                0,
                "daily_bars",
                json.dumps({"date": "20260105"}),
                "2026-02-01T00:00:00+00:00",
                "2026-02-01",
                gzip.compress(json.dumps(raw).encode(), mtime=0),
            ),
        )
    mirror = tmp_path / "mirror"
    store = MemoryR2Store()
    with publication.prepare_market_lake(
        sqlite_path=database, mirror_root=mirror, store=store, release_id="price-from-l1"
    ) as prepared:
        publication.publish_prepared_market_lake(prepared, store=store)
        derived = prepared.export.datasets["jquants.daily_bars"].manifest
        assert all(src.kind == "l1_dataset" for part in derived.partitions for src in part.sources)
        paths = materialize_partitions(
            LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
            dataset=LAKE_DATASETS["jquants.daily_bars"],
            partitions=derived.partitions,
        )
        with duckdb.connect(":memory:") as conn:
            assert conn.execute(
                "SELECT ticker, close FROM read_parquet(?)", [[str(p) for p in paths]]
            ).fetchall() == [("1301", 100.0)]
    assert "lake/pointers/market/current.json" in store.values


def test_market_publication_derives_financials_and_prices_from_same_l1(tmp_path, monkeypatch):
    from tests.helpers.r2_store import MemoryR2Store

    import baibai_jobs.publication.snapshot_publication as publication

    names = ("jquants.source_pages", "jquants.daily_bars", "jquants.fin_summaries")
    narrow_release_policy(monkeypatch, datasets=names)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {k: LAKE_DATASETS[k] for k in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    database = market_store(
        tmp_path / "market.sqlite", daily_bars=(("1301", "2026-01-05", 999.0, 1.0),)
    )
    payloads = (
        ("daily_bars", {"Code": "13010", "Date": "2026-01-05", "C": 100}),
        ("financial_summaries", {"Code": "13010", "DiscDate": "2026-01-05", "DiscNo": "filing"}),
    )
    with closing(open_connection(database)) as conn, conn:
        for source, record in payloads:
            conn.execute(
                "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    source,
                    0,
                    source,
                    json.dumps({"date": "20260105"}),
                    "2026-02-01T00:00:00+00:00",
                    "2026-02-01",
                    gzip.compress(json.dumps({"data": [record]}).encode(), mtime=0),
                ),
            )
        conn.execute(
            "INSERT INTO jquants_fin_summaries VALUES (?, ?, ?, ?)",
            ("9999", "2026-01-05", "old-l2", "{}"),
        )
    mirror, store = tmp_path / "mirror", MemoryR2Store()
    with publication.prepare_market_lake(
        sqlite_path=database, mirror_root=mirror, store=store, release_id="both-from-l1"
    ) as prepared:
        l1 = prepared.export.datasets["jquants.source_pages"].manifest
        for name in names[1:]:
            manifest = prepared.export.datasets[name].manifest
            assert all(
                ref.kind == "l1_dataset" and ref.build_id == l1.build_id
                for part in manifest.partitions
                for ref in part.sources
            )
        publication.publish_prepared_market_lake(prepared, store=store)
    assert "lake/pointers/market/current.json" in store.values
