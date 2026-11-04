from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, date, datetime

from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store

from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.jquants_financial_plan import plan_fixed_financial_acquisition
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
    with lake_session() as session:
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
