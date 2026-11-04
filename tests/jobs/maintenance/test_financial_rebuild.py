from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, date, datetime

from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.contract import JQUANTS_SOURCE_PAGES_DDL
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.jquants_financial_plan import (
    freeze_financial_acquisition_plan,
    plan_fixed_financial_acquisition,
)
from baibai_jobs.maintenance.financial_rebuild import rebuild_fixed_financial_days
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.policy import release_policy_for_profile


def test_fixed_l1_rebuild_needs_no_previous_l2_or_sqlite(tmp_path, monkeypatch) -> None:
    narrow_release_policy(monkeypatch, datasets=("jquants.source_pages",), carries_history=None)
    source_path = market_store(tmp_path / "source.sqlite")
    with open_connection(source_path) as conn:
        for identity, query_day, received in [
            ("old", "20261102", "2026-11-03T00:00:00.000000+00:00"),
            ("future", "20261103", "2026-12-01T00:00:00.000000+00:00"),
        ]:
            record = {
                "Code": "13010",
                "DiscDate": date.fromisoformat(query_day).isoformat(),
                "DiscNo": identity,
            }
            body = gzip.compress(json.dumps({"data": [record]}).encode(), mtime=0)
            conn.execute(
                "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    identity,
                    0,
                    "financial_summaries",
                    json.dumps({"date": query_day}),
                    received,
                    received[:10],
                    body,
                ),
            )
    mirror = tmp_path / "mirror"
    with sealed_sqlite_snapshot(sqlite_path=source_path, mirror_root=mirror) as snapshot:
        manifest = export_dataset_snapshot(
            dataset=LAKE_DATASETS["jquants.source_pages"],
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            source_snapshot=snapshot,
            build_id="l1-only-test",
        )
    release_path, _ = create_release(
        dataset_manifest_paths=[manifest.manifest_path],
        mirror_root=mirror,
        release_id="l1-only-release",
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    source = LocalMirrorSource(mirror)
    release = resolve_release(
        source,
        release_id="l1-only-release",
        manifest_sha256=hashlib.sha256(release_path.read_bytes()).hexdigest(),
    )
    source_path.unlink()
    pending = tmp_path / "pending.sqlite"
    with sqlite3.connect(pending) as connection:
        connection.executescript(JQUANTS_SOURCE_PAGES_DDL)
        body = gzip.compress(json.dumps({"data": []}).encode(), mtime=0)
        connection.execute(
            "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "pending",
                0,
                "financial_summaries",
                json.dumps({"date": "20261103"}),
                "2026-11-03T01:00:00.000000+00:00",
                "2026-11-03",
                body,
            ),
        )
    stage_dir = tmp_path / "stage"
    stage_dir.mkdir()
    shutil.copyfile(pending, stage_dir / "receipts.sqlite")
    (stage_dir / "result.json").write_text(
        json.dumps(
            {
                "source": "jquants-financial",
                "asof": "2026-11-03",
                "started_at": "2026-11-03T01:00:00+00:00",
                "request_ids": ["pending"],
            }
        )
    )
    store = MemoryR2Store()
    save_handoff(store, "jquants-financial-pending", stage_dir, market=True)
    store.store_object("coordination/market-sources/jquants-financial-invalid/handoff.json", b"{}")
    with lake_session() as session:
        frozen = freeze_financial_acquisition_plan(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=source),
            store=store,
            start=date(2026, 11, 2),
            end=date(2026, 11, 4),
            revision_overlap_days=0,
            clock=lambda: datetime(2026, 11, 4, 2, tzinfo=UTC),
        )
        assert frozen.result.plan.days == (date(2026, 11, 4),)
        assert frozen.pending_stages == ("jquants-financial-pending",)
        assert frozen.unavailable_stages == ("jquants-financial-invalid",)
        assert frozen.result.plan.unknown_incomplete_requests == (
            "stage:jquants-financial-invalid",
        )
        with_pending = plan_fixed_financial_acquisition(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=source),
            start=date(2026, 11, 2),
            end=date(2026, 11, 4),
            known_at=datetime(2026, 11, 4, tzinfo=UTC),
            revision_overlap_days=0,
            pending_paths=[pending],
        )
        assert with_pending.plan.days == (date(2026, 11, 4),)
        plan = plan_fixed_financial_acquisition(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=source),
            start=date(2026, 11, 2),
            end=date(2026, 11, 3),
            known_at=datetime(2026, 11, 4, tzinfo=UTC),
            revision_overlap_days=0,
        )
        assert plan.plan.days == (date(2026, 11, 3),)
        assert plan.l1_coverage_status == "partial"
        report = rebuild_fixed_financial_days(
            session,
            release=release,
            cache=LakeObjectCache(root=tmp_path / "cache", source=source),
            days=[date(2026, 11, 2), date(2026, 11, 3)],
            known_at=datetime(2026, 11, 4, tzinfo=UTC),
        )
        rows = session.connection.execute(
            "SELECT ticker, disclosed_at, disclosure_number FROM financial_summaries"
        ).fetchall()
        status = session.connection.execute(
            "SELECT day, projection_status FROM financial_receipt_status ORDER BY day"
        ).fetchall()
    assert rows == [("1301", "2026-11-02", "old")]
    assert status == [(date(2026, 11, 2), "ok"), (date(2026, 11, 3), "unavailable")]
    assert report.l1_coverage_status == "partial"
    assert report.rows == 1
    assert report.unavailable_days == 1


def test_financial_input_is_not_published_after_l1_cutover(tmp_path, monkeypatch):
    from baibai_jobs.acquisition import source_inputs

    remote = MemoryR2Store()
    monkeypatch.setattr(source_inputs, "SOURCE_INPUT_GROUPS", ("jquants-financial",))
    monkeypatch.setattr(source_inputs, "economic_acquisition_groups", lambda: ())
    result = source_inputs.publish_inputs(
        remote,
        market=tmp_path / "not-read-market.sqlite",
        economic=tmp_path / "not-read-economic.sqlite",
        identity="batch",
        asof=date(2026, 11, 4),
        generations={},
    )
    assert result == {"updated": [], "failed": []}
    assert remote.values == {}
