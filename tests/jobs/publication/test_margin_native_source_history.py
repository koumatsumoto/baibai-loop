"""Margin-source cutovers keep the remaining mixed-original history floor."""

import gzip
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.lake_release import market_store
from tests.helpers.margin_alerts_native import attempt as alert_attempt
from tests.helpers.master_calendar_native import (
    native_master_calendar_publication as native_master_calendar_publication,
)
from tests.helpers.price_native import native_price_publication as native_price_publication
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.short_sale_reports_native import attempt as short_attempt

import baibai_jobs.publication.snapshot_publication as publication
from baibai_engine.data_collection.providers.jquants.margin_alerts_native import MarginAlertsL1
from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    ShortSaleReportsL1,
)
from baibai_engine.machine_analysis.observations.jquants_writer import (
    store_jquants_margin_alerts,
    store_jquants_short_sale_reports,
)
from baibai_jobs.publication.datasets import LAKE_DATASETS

DAY = date(2026, 10, 1)
AT = datetime(2026, 10, 1, tzinfo=UTC)
POINTER = "lake/pointers/market/current.json"


@pytest.fixture(params=["margin_alerts", "short_sale_reports"])
def native_source(request):
    return request.param


@pytest.fixture
def native_margin_release(tmp_path, monkeypatch, native_source):
    names = ("jquants.source_pages", f"jquants.{native_source}")
    narrow_release_policy(monkeypatch, datasets=names, carries_history=None)
    monkeypatch.setattr(publication, "LAKE_DATASETS", {name: LAKE_DATASETS[name] for name in names})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)

    def create(remaining=(), *, migrated=True, both=False):
        store = MemoryR2Store()
        market = market_store(tmp_path / "market.sqlite")
        sources = ("margin_alerts", "short_sale_reports") if both else (native_source,)
        if "short_sale_reports" in sources:
            with sqlite3.connect(market) as connection:
                connection.execute("DELETE FROM jquants_short_sale_reports")
                connection.execute(
                    "DELETE FROM source_coverage WHERE source='jquants_short_sale_reports'"
                )
        for source in sources:
            if source == "margin_alerts":
                owner = MarginAlertsL1(store, tmp_path / source)
                received = alert_attempt(source, day=DAY, at=AT)
                writer = store_jquants_margin_alerts
            else:
                owner = ShortSaleReportsL1(store, tmp_path / source)
                received = short_attempt(source, day=DAY, at=AT)
                writer = store_jquants_short_sale_reports
            owner.initialize()
            received = replace(received, attempted_at=None)
            if migrated:
                owner.commit([received])
            with sqlite3.connect(market) as connection:
                for index, page in enumerate(received.pages):
                    connection.execute(
                        "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
                        (
                            received.request_id,
                            index,
                            page.source,
                            json.dumps(dict(page.query)),
                            page.received_at.isoformat(),
                            page.received_at.date().isoformat(),
                            gzip.compress(page.body, mtime=0),
                        ),
                    )
            writer(
                market,
                json.loads(received.pages[0].body)["data"],
                requested_start=DAY,
                requested_end=DAY,
                fetched_at_utc=AT.isoformat(),
            )
        with sqlite3.connect(market) as connection:
            for index, (source, received_on) in enumerate(remaining):
                connection.execute(
                    "INSERT INTO jquants_source_pages VALUES (?,?,?,?,?,?,?)",
                    (
                        f"remaining-{index}",
                        0,
                        source,
                        "{}",
                        received_on + "T00:00:00+00:00",
                        received_on,
                        gzip.compress(b'{"data":[]}', mtime=0),
                    ),
                )
        mirror = tmp_path / "mirror"
        # A historical release is made from the old SQLite projection, without
        # invoking the current native-only publication route.
        from baibai_engine.storage.snapshots.release import create_release
        from baibai_engine.storage.snapshots.writer import export_market_store
        from baibai_jobs.publication.policy import release_policy_for_profile
        from baibai_jobs.publication.snapshot_release import publish_release

        exported = export_market_store(
            sqlite_path=market,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
            catalog=publication.LAKE_DATASETS,
            expected_store_origin=None,
        )
        release_path, _ = create_release(
            dataset_manifest_paths=[item.manifest_path for item in exported.datasets.values()],
            mirror_root=mirror,
            release_id="old-margin",
            catalog=publication.LAKE_DATASETS,
            policy=release_policy_for_profile("production"),
        )
        publish_release(mirror_root=mirror, release_manifest_path=release_path, store=store)
        from baibai_engine.storage.sqlite.source_cache.lake_origin import (
            LakeStoreOrigin,
            write_lake_store_origin,
        )
        from baibai_jobs.publication.snapshot_publication import _serving_pointer

        pointer = _serving_pointer(store).pointer
        with sqlite3.connect(market) as connection:
            write_lake_store_origin(
                connection,
                LakeStoreOrigin(
                    release_id=pointer.release_id, release_manifest_sha256=pointer.manifest_sha256
                ),
            )
        originals = {key: item.body for key, item in store.values.items() if key != POINTER}
        return store, market, mirror, originals

    return create


def test_margin_cutover_uses_remaining_floor_and_preserves_originals(
    native_margin_release, native_source, monkeypatch
):
    store, market, mirror, originals = native_margin_release(
        [("financial_summaries", "2026-10-02")]
    )
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="native-margin"
    ) as prepared:
        assert prepared.export.datasets["jquants.source_pages"].manifest.coverage_start == date(
            2026, 10, 2
        )
        publication.publish_prepared_market_lake(prepared, store=store)
    assert all(store.values[key].body == body for key, body in originals.items())
    with sqlite3.connect(market) as connection:
        assert connection.execute("SELECT source FROM jquants_source_pages").fetchall() == [
            ("financial_summaries",)
        ]


@pytest.mark.parametrize("lost_source", ["financial_summaries"])
@pytest.mark.parametrize("lose_all", [False, True])
def test_margin_cutover_does_not_exempt_remaining_originals(
    native_margin_release, native_source, monkeypatch, lost_source, lose_all
):
    rows = [(lost_source, "2026-10-02")]
    if not lose_all:
        rows.append(("weekly_margin", "2026-10-03"))
    store, market, mirror, _ = native_margin_release(rows)
    pointer = store.values[POINTER].body
    with sqlite3.connect(market) as connection:
        connection.execute("DELETE FROM jquants_source_pages WHERE source=?", (lost_source,))
    message = "historical dataset" if lose_all else "coverage starts 2026-10-03"
    with (
        pytest.raises(ValueError, match=message),
        publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="lost"
        ),
    ):
        pytest.fail("remaining originals must retain their old history floor")
    assert store.values[POINTER].body == pointer


def test_margin_native_only_pages_may_leave_optional_dataset(
    native_margin_release, native_source, monkeypatch
):
    store, market, mirror, originals = native_margin_release()
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="only-native"
    ) as prepared:
        assert "jquants.source_pages" not in prepared.release.datasets
        publication.publish_prepared_market_lake(prepared, store=store)
    assert all(store.values[key].body == body for key, body in originals.items())


def test_unmigrated_margin_receipt_still_blocks_before_history_adjustment(
    native_margin_release, native_source, monkeypatch
):
    store, market, mirror, _ = native_margin_release(
        [("financial_summaries", "2026-10-02")], migrated=False
    )
    pointer = store.values[POINTER].body
    with (
        pytest.raises(ValueError, match="absent from or differs from the native cutover"),
        publication.prepare_market_lake(
            sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY, release_id="unmigrated"
        ),
    ):
        pytest.fail("an enabled flag does not prove the raw receipt was migrated")
    assert store.values[POINTER].body == pointer
    with sqlite3.connect(market) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM jquants_source_pages WHERE source=?", (native_source,)
        ).fetchone() == (1,)


pytestmark = pytest.mark.usefixtures(
    "native_price_publication", "native_master_calendar_publication"
)
