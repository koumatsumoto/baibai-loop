from __future__ import annotations

import gzip
import hashlib
import json
from datetime import UTC, date, datetime

from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store

from baibai_engine.data_collection.providers.jquants.lake_receipts import (
    iter_fixed_retained_requests,
)
from baibai_engine.machine_analysis.observations.financials.from_l1 import (
    materialize_financial_days,
    rebuild_financial_days,
)
from baibai_engine.machine_analysis.observations.prices.from_l1 import (
    materialize_price_days,
    rebuild_price_days,
)
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import resolve_release
from baibai_engine.storage.snapshots.release import create_release
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
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
    with open_connection(source_path) as conn:
        price_body = gzip.compress(
            json.dumps({"data": [{"Code": "13010", "Date": "2026-11-02", "C": 100}]}).encode(),
            mtime=0,
        )
        incomplete_body = gzip.compress(
            json.dumps({"data": [], "pagination_key": "next"}).encode(), mtime=0
        )
        conn.execute(
            "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "incomplete",
                0,
                "daily_bars",
                json.dumps({"date": "2026-11-01"}),
                "2026-11-03T00:00:00.000000+00:00",
                "2026-11-03",
                incomplete_body,
            ),
        )
        conn.execute(
            "INSERT INTO jquants_source_pages VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "price",
                0,
                "daily_bars",
                json.dumps({"date": "2026-11-02"}),
                "2026-11-03T00:00:00.000000+00:00",
                "2026-11-03",
                price_body,
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
        cache = LakeObjectCache(root=tmp_path / "cache", source=source)
        financial_requests = tuple(
            iter_fixed_retained_requests(
                session,
                release=release,
                cache=cache,
                source="financial_summaries",
                known_at=datetime(2026, 11, 4, tzinfo=UTC),
            )
        )
        price_requests = tuple(
            iter_fixed_retained_requests(
                session,
                release=release,
                cache=cache,
                source="daily_bars",
                known_at=datetime(2026, 11, 4, tzinfo=UTC),
            )
        )
        assert [request.request_id for request in financial_requests] == ["old"]
        assert [request.request_id for request in price_requests] == ["incomplete", "price"]
        assert price_requests[0].request is None
        financial = rebuild_financial_days(
            financial_requests, days=[date(2026, 11, 2), date(2026, 11, 3)]
        )
        prices = rebuild_price_days(price_requests, days=[date(2026, 11, 1), date(2026, 11, 2)])
        coverage = release.dataset_manifest("jquants.source_pages").coverage_status
        materialize_financial_days(
            session.connection, financial.values(), l1_coverage_status=coverage
        )
        materialize_price_days(session.connection, prices.values(), l1_coverage_status=coverage)
        assert session.connection.execute("SELECT ticker, close FROM daily_bars").fetchall() == [
            ("1301", 100.0)
        ]
        rows = session.connection.execute(
            "SELECT ticker, disclosed_at, disclosure_number FROM financial_summaries"
        ).fetchall()
        status = session.connection.execute(
            "SELECT day, projection_status FROM financial_receipt_status ORDER BY day"
        ).fetchall()
    assert rows == [("1301", "2026-11-02", "old")]
    assert status == [(date(2026, 11, 2), "ok"), (date(2026, 11, 3), "unavailable")]
    assert coverage == "partial"
    assert sum(len(item.rows) for item in financial.values()) == 1
    assert financial[date(2026, 11, 3)].status == "unavailable"
    assert prices[date(2026, 11, 1)].incomplete_requests == 1


def test_financial_input_is_not_published_after_l1_cutover():
    from baibai_jobs.acquisition.inputs import SOURCE_INPUT_GROUPS

    assert "jquants-financial" not in SOURCE_INPUT_GROUPS
