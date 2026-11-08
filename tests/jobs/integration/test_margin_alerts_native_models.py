"""A fixed native input powers cold, cached and published margin-alert readers."""

import sqlite3
from datetime import date, timedelta

import pytest
from tests.helpers.margin_alerts_native import AT, DAY, attempt, records
from tests.helpers.master_calendar_native import (
    native_master_calendar_publication as native_master_calendar_publication,
)
from tests.helpers.price_native import native_price_publication as native_price_publication
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.jquants.margin_alerts_native import (
    HEAD_KEY,
    PREFIX,
    MarginAlertsL1,
)
from baibai_engine.machine_analysis.observations.trading_activity.margin_reader import (
    read_margin_alerts,
)
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.integration.margin_alerts_native import materialize_saved_input, model_input
from baibai_jobs.publication.l1_margin_alerts_export import export_margin_alerts_from_native_input


def fixture(tmp_path, *, earlier=None):
    store = MemoryR2Store()
    owner = MarginAlertsL1(store, tmp_path / "native")
    owner.initialize()
    start = earlier or DAY
    owner.commit(
        [
            attempt(
                "initial-" + day.isoformat(),
                day=day,
                rows=records(day) if day in {start, DAY} else [],
            )
            for day in (start + timedelta(days=n) for n in range((DAY - start).days + 1))
        ]
    )
    market, mirror = tmp_path / "market.sqlite", tmp_path / "mirror"
    connection = open_connection(market)
    connection.close()
    return store, owner, market, mirror


def build(store, owner, market, mirror, *, fixed=None, previous=None, at=AT, as_of=DAY):
    saved = model_input(
        store,
        fixed or owner.fixed(),
        as_of=as_of,
        evaluated_at=at,
        market=market,
        mirror_root=mirror,
        previous=previous,
    )
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as snapshot:
        report = export_margin_alerts_from_native_input(
            source_snapshot=snapshot, saved=saved, mirror_root=mirror, producer_git_commit="a" * 40
        )
    for part in report.manifest.partitions:
        for obj in part.objects:
            store.store_object(obj.key, (mirror / obj.key).read_bytes(), sha256=obj.sha256)
    return saved, report


def rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT * FROM jquants_margin_alerts ORDER BY publication_date,ticker,applied_date"
        ).fetchall()


def test_equal_recheck_uses_existing_l2_but_not_old_confirmation(tmp_path):
    store, owner, market, mirror = fixture(tmp_path, earlier=date(2026, 10, 1))
    _, first = build(store, owner, market, mirror)
    owner.commit([attempt("equal", at=AT + timedelta(hours=1))])
    fixed = owner.fixed()
    store.get_keys.clear()
    saved, second = build(
        store,
        owner,
        market,
        mirror,
        fixed=fixed,
        previous=first.manifest,
        at=AT + timedelta(hours=1),
    )
    assert not [key for key in store.get_keys if key.startswith(PREFIX + "packs/")]
    assert HEAD_KEY not in store.get_keys
    assert second.new_objects == 0
    cold = tmp_path / "cold.sqlite"
    materialize_saved_input(store, saved, output=cold)
    assert rows(market) == rows(cold)
    assert saved.descriptor["summary"]["coverage_status"] == "complete"


@pytest.mark.parametrize("damage", ["rows", "deleted", "extra", "objects", "partial"])
def test_corrupt_or_absent_l2_rebuilds_same_fixed_input(tmp_path, damage):
    store, owner, market, mirror = fixture(tmp_path)
    saved, previous = build(store, owner, market, mirror)
    frozen = owner.fixed()
    expected = rows(market)
    if damage in {"rows", "deleted", "extra"}:
        with sqlite3.connect(market) as connection:
            if damage == "rows":
                connection.execute("UPDATE jquants_margin_alerts SET long_outstanding=999")
            elif damage == "deleted":
                connection.execute("DELETE FROM jquants_margin_alerts")
            else:
                connection.execute(
                    "INSERT INTO jquants_margin_alerts(publication_date,ticker,applied_date) VALUES ('2020-01-01','9999','2020-01-01')"
                )
    elif damage == "objects":
        for part in previous.manifest.partitions:
            for obj in part.objects:
                (mirror / obj.key).unlink()
                del store.values[obj.key]
    previous_manifest = previous.manifest
    if damage == "partial":
        previous_manifest = previous_manifest.model_copy(update={"coverage_status": "partial"})
    owner.commit([attempt("new-head", at=AT + timedelta(hours=1), rows=records(long=9999))])
    store.get_keys.clear()
    rebuilt = model_input(
        store,
        frozen,
        as_of=DAY,
        evaluated_at=AT,
        market=market,
        mirror_root=mirror,
        previous=previous_manifest,
    )
    assert rows(market) == expected
    assert rebuilt == saved
    assert HEAD_KEY not in store.get_keys
    assert any(key.startswith(PREFIX + "packs/") for key in store.get_keys)


def test_empty_correction_replaces_only_its_pubdate_and_keeps_appdate(tmp_path):
    prior = DAY - timedelta(days=1)
    store, owner, market, mirror = fixture(tmp_path, earlier=prior)
    owner.commit(
        [
            attempt(
                "two-applications",
                at=AT + timedelta(hours=1),
                rows=[*records(application=prior), *records(application=DAY)],
            )
        ]
    )
    _, previous = build(store, owner, market, mirror, at=AT + timedelta(hours=1))
    assert len(rows(market)) == 3
    owner.commit([attempt("clear", day=prior, at=AT + timedelta(hours=2), rows=[])])
    saved, report = build(
        store, owner, market, mirror, previous=previous.manifest, at=AT + timedelta(hours=2)
    )
    assert len(rows(market)) == 2
    result = read_margin_alerts(market, prior, DAY)
    assert [(row.publication_date, row.applied_date) for row in result] == [
        (DAY, prior),
        (DAY, DAY),
    ]
    assert saved.descriptor["summary"]["coverage_status"] == "complete"
    assert report.manifest.coverage_status == "complete"


def test_changed_month_reuses_older_object_and_reads_only_changed_raw(tmp_path):
    store, owner, market, mirror = fixture(tmp_path, earlier=date(2026, 10, 1))
    _, first = build(store, owner, market, mirror)
    old_keys = {
        obj.min_key[0][:7]: obj.key for part in first.manifest.partitions for obj in part.objects
    }
    owner.commit([attempt("changed", at=AT + timedelta(hours=1), rows=records(long=3456))])
    fixed = owner.fixed()
    store.get_keys.clear()
    saved, second = build(
        store,
        owner,
        market,
        mirror,
        fixed=fixed,
        previous=first.manifest,
        at=AT + timedelta(hours=1),
    )
    new_keys = {
        obj.min_key[0][:7]: obj.key for part in second.manifest.partitions for obj in part.objects
    }
    assert old_keys["2026-10"] == new_keys["2026-10"]
    assert old_keys["2026-11"] != new_keys["2026-11"]
    assert second.new_objects == 1
    assert len([key for key in store.get_keys if key.startswith(PREFIX + "packs/")]) == 1
    cold = tmp_path / "cold.sqlite"
    materialize_saved_input(store, saved, output=cold)
    assert rows(cold) == rows(market)


def test_failed_refresh_is_partial_but_keeps_adopted_rows(tmp_path):
    store, owner, market, mirror = fixture(tmp_path)
    _, first = build(store, owner, market, mirror)
    expected = rows(market)
    owner.commit([attempt("partial", at=AT + timedelta(hours=1), partial=True)])
    saved, second = build(
        store, owner, market, mirror, previous=first.manifest, at=AT + timedelta(hours=1)
    )
    assert rows(market) == expected
    assert saved.descriptor["summary"]["coverage_status"] == "partial"
    assert second.manifest.coverage_status == "partial"
    assert read_margin_alerts(market, DAY, DAY) is None


def test_normal_empty_is_readable_without_fabricating_a_lake_row(tmp_path):
    store, owner, market, mirror = fixture(tmp_path)
    owner.commit([attempt("empty", at=AT + timedelta(hours=1), rows=[])])
    saved = model_input(
        store,
        owner.fixed(),
        as_of=DAY,
        evaluated_at=AT + timedelta(hours=1),
        market=market,
        mirror_root=mirror,
    )
    assert rows(market) == []
    assert read_margin_alerts(market, DAY, DAY) == []
    assert saved.descriptor["summary"]["coverage_status"] == "complete"


def test_normal_publication_fixes_one_head_for_seal_reader_and_lineage(tmp_path, monkeypatch):
    from tests.helpers.lake_policy import narrow_release_policy

    from baibai_jobs.publication import snapshot_publication as publication
    from baibai_jobs.publication.datasets import LAKE_DATASETS

    store, owner, market, mirror = fixture(tmp_path)
    name = "jquants.margin_alerts"
    narrow_release_policy(monkeypatch, datasets=(name,))
    monkeypatch.setattr(publication, "LAKE_DATASETS", {name: LAKE_DATASETS[name]})
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "a" * 40)
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY
    ) as prepared:
        fixed_input = prepared.margin_alerts_identity
        assert fixed_input is not None
        snapshot_rows = rows(prepared.source_snapshot.path)
        assert snapshot_rows == rows(market)
        manifest = prepared.export.datasets[name].manifest
        assert {source.sha256 for part in manifest.partitions for source in part.sources} == {
            fixed_input.descriptor["selection_sha256"]
        }
        assert all(
            source.kind == "native_l1" for part in manifest.partitions for source in part.sources
        )
        owner.commit([attempt("later", at=AT + timedelta(hours=2), rows=records(long=999))])
        store.get_keys.clear()
        published = publication.publish_prepared_market_lake(prepared, store=store)
        assert HEAD_KEY not in store.get_keys
        assert rows(prepared.source_snapshot.path) == snapshot_rows
    assert published.uploaded_objects > 0
    # A second normal run sees the newer original, and seals the new identity once.
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY
    ) as prepared:
        assert prepared.margin_alerts_identity != fixed_input
        assert rows(prepared.source_snapshot.path) != snapshot_rows


pytestmark = pytest.mark.usefixtures(
    "native_price_publication", "native_master_calendar_publication"
)
