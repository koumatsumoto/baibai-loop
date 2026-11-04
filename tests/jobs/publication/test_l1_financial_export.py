from __future__ import annotations

import gzip
import hashlib
import json
from contextlib import closing
from datetime import UTC, datetime

import duckdb
import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store

from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import materialize_partitions, resolve_release
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.l1_financial_export import export_financial_from_l1
from baibai_jobs.publication.policy import release_policy_for_profile


def native_financial_source(tmp_path, dates):
    source = market_store(tmp_path / "source.sqlite")
    with closing(open_connection(source)) as conn, conn:
        for day, rows in dates:
            body = gzip.compress(json.dumps(rows if isinstance(rows, dict) else {"data": rows}).encode(), mtime=0)
            conn.execute(
                "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    day,
                    0,
                    "financial_summaries",
                    json.dumps({"date": day}),
                    "2026-02-01T00:00:00+00:00",
                    "2026-02-01",
                    body,
                ),
            )
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=source, mirror_root=mirror) as snapshot:
        l1 = export_dataset_snapshot(
            dataset=LAKE_DATASETS["jquants.source_pages"],
            mirror_root=mirror,
            source_snapshot=snapshot,
            producer_git_commit="a" * 40,
            build_id="source",
            created_at=datetime(2026, 2, 4, tzinfo=UTC),
        )
    source.unlink()
    return mirror, l1


def test_financial_export_uses_only_l1_and_publishes_exact_rows(tmp_path, monkeypatch):
    names = ("jquants.source_pages", "jquants.fin_summaries")
    narrow_release_policy(monkeypatch, datasets=names)
    row = {"Code": "13010", "DiscDate": "2026-01-05", "DiscNo": "filing"}
    mirror, l1 = native_financial_source(tmp_path, [("20260105", [row]), ("20260106", [])])
    when = datetime(2026, 2, 4, tzinfo=UTC)
    report = export_financial_from_l1(
        source=l1,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        known_at=when,
        build_id="derived",
        created_at=when,
    )
    assert (report.source_days, report.missing_days, report.partial_days) == (2, 0, 0)
    assert report.build.manifest.coverage_status == "complete"
    release_path, release = create_release(
        dataset_manifest_paths=[l1.manifest_path, report.build.manifest_path],
        mirror_root=mirror,
        release_id="financial-direct",
        created_at=when,
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    resolved = resolve_release(
        LocalMirrorSource(mirror),
        release_id=release.release_id,
        manifest_sha256=hashlib.sha256(release_path.read_bytes()).hexdigest(),
    )
    paths = materialize_partitions(
        LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
        dataset=LAKE_DATASETS[names[1]],
        partitions=resolved.dataset_manifest(names[1]).partitions,
    )
    with duckdb.connect(":memory:") as conn:
        assert conn.execute(
            "SELECT ticker, disclosed_at, disclosure_number FROM read_parquet(?)",
            [[str(p) for p in paths]],
        ).fetchall() == [("1301", "2026-01-05", "filing")]


def test_financial_export_does_not_claim_complete_with_missing_day(tmp_path, monkeypatch):
    names = ("jquants.source_pages", "jquants.fin_summaries")
    narrow_release_policy(monkeypatch, datasets=names)
    rows = [{"Code": "13010", "DiscDate": "2026-01-05", "DiscNo": "filing"}]
    mirror, l1 = native_financial_source(tmp_path, [("20260105", rows), ("20260107", [])])
    when = datetime(2026, 2, 4, tzinfo=UTC)
    report = export_financial_from_l1(
        source=l1,
        mirror_root=mirror,
        producer_git_commit="a" * 40,
        known_at=when,
        build_id="partial",
        created_at=when,
    )
    assert report.missing_days == 1
    assert report.build.manifest.coverage_status == "partial"
    with pytest.raises(ValueError, match="coverage is partial"):
        create_release(
            dataset_manifest_paths=[l1.manifest_path, report.build.manifest_path],
            mirror_root=mirror,
            release_id="partial-release",
            created_at=when,
            catalog=LAKE_DATASETS,
            policy=release_policy_for_profile("production"),
        )


def test_later_incomplete_request_prevents_complete_publication(tmp_path, monkeypatch):
    names = ("jquants.source_pages", "jquants.fin_summaries")
    narrow_release_policy(monkeypatch, datasets=names)
    row = {"Code": "13010", "DiscDate": "2026-01-05", "DiscNo": "filing"}
    mirror, l1 = native_financial_source(
        tmp_path,
        [("20260105", [row]), ("20260106", {"data": [], "pagination_key": "next"})],
    )
    when = datetime(2026, 2, 4, tzinfo=UTC)
    report = export_financial_from_l1(
        source=l1, mirror_root=mirror, producer_git_commit="a" * 40,
        known_at=when, build_id="partial-latest", created_at=when,
    )
    assert report.missing_days == 1
    assert report.build.manifest.coverage_status == "partial"
    with pytest.raises(ValueError, match="coverage is partial"):
        create_release(
            dataset_manifest_paths=[l1.manifest_path, report.build.manifest_path],
            mirror_root=mirror, release_id="partial-latest", created_at=when,
            catalog=LAKE_DATASETS, policy=release_policy_for_profile("production"),
        )


def test_market_publish_financial_rows_come_from_l1_not_old_sqlite(tmp_path, monkeypatch):
    from tests.helpers.r2_store import MemoryR2Store

    import baibai_jobs.publication.snapshot_publication as publication

    names = ("jquants.source_pages", "jquants.fin_summaries")
    narrow_release_policy(monkeypatch, datasets=names)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {k: LAKE_DATASETS[k] for k in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    database = market_store(tmp_path / "market.sqlite")
    row = {"Code": "13010", "DiscDate": "2026-01-05", "DiscNo": "l1-filing"}
    body = gzip.compress(json.dumps({"data": [row]}).encode(), mtime=0)
    with closing(open_connection(database)) as conn, conn:
        conn.execute(
            "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("request-1", 0, "financial_summaries", json.dumps({"date": "20260105"}),
             "2026-02-01T00:00:00+00:00", "2026-02-01", body),
        )
        conn.execute(
            "INSERT INTO jquants_fin_summaries VALUES (?, ?, ?, ?)",
            ("9999", "2026-01-05", "old-l2", "{}"),
        )
    mirror = tmp_path / "mirror"
    store = MemoryR2Store()
    with publication.prepare_market_lake(
        sqlite_path=database, mirror_root=mirror, store=store, release_id="direct-l1"
    ) as prepared:
        report = publication.publish_prepared_market_lake(prepared, store=store)
        assert report.release_id == "direct-l1"
        derived = prepared.export.datasets["jquants.fin_summaries"].manifest
        assert all(src.kind == "l1_dataset" for part in derived.partitions for src in part.sources)
        paths = materialize_partitions(
            LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
            dataset=LAKE_DATASETS["jquants.fin_summaries"], partitions=derived.partitions,
        )
        with duckdb.connect(":memory:") as conn:
            assert conn.execute(
                "SELECT ticker, disclosure_number FROM read_parquet(?)", [[str(p) for p in paths]]
            ).fetchall() == [("1301", "l1-filing")]
    assert "lake/pointers/market/current.json" in store.values


def test_market_prepare_rejects_later_incomplete_source_without_publishing(tmp_path, monkeypatch):
    from tests.helpers.r2_store import MemoryR2Store

    import baibai_jobs.publication.snapshot_publication as publication

    names = ("jquants.source_pages", "jquants.fin_summaries")
    narrow_release_policy(monkeypatch, datasets=names)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {k: LAKE_DATASETS[k] for k in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    database = market_store(tmp_path / "market.sqlite")
    complete = {"data": [{"Code": "13010", "DiscDate": "2026-01-05", "DiscNo": "one"}]}
    incomplete = {"data": [], "pagination_key": "not-received"}
    with closing(open_connection(database)) as conn, conn:
        for day, payload in (("20260105", complete), ("20260106", incomplete)):
            conn.execute(
                "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (day, 0, "financial_summaries", json.dumps({"date": day}),
                 "2026-02-01T00:00:00+00:00", "2026-02-01",
                 gzip.compress(json.dumps(payload).encode(), mtime=0)),
            )
    store = MemoryR2Store()
    with (
        pytest.raises(ValueError, match="coverage is partial"),
        publication.prepare_market_lake(
            sqlite_path=database, mirror_root=tmp_path / "mirror", store=store
        ),
    ):
        pytest.fail("partial source must not prepare a release")
    assert "lake/pointers/market/current.json" not in store.values
