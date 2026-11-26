"""A fixed native input powers cold, cached and published margin-alert readers."""

import sqlite3
from contextlib import closing
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
from baibai_engine.machine_analysis.generation.margin_alerts_inputs import (
    INPUT_PREFIX,
)
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.coverage import uncovered_days
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


def test_publication_date_cannot_escape_its_partition_or_claim_empty_coverage(tmp_path):
    # The compact raw key sorts inside this cross-year selection, but does not
    # belong to either canonical day. Even an older disposable copy may contain it.
    first, last = date(2025, 12, 31), date(2026, 1, 1)
    store = MemoryR2Store()
    owner = MarginAlertsL1(store, tmp_path / "native")
    owner.initialize()
    raw = attempt("ambiguous", day=first, rows=[{**records(first)[0], "PubDate": "20251231"}])
    owner.commit([raw, attempt("valid", day=last)])
    market, mirror = tmp_path / "market.sqlite", tmp_path / "mirror"
    with closing(open_connection(market)) as connection, connection:
        connection.execute(
            "INSERT INTO jquants_margin_alerts(publication_date,ticker,applied_date) "
            "VALUES ('20251231','9999','2025-12-31')"
        )

    saved = model_input(
        store, owner.fixed(), as_of=last, evaluated_at=AT, market=market, mirror_root=mirror
    )
    cold = tmp_path / "cold.sqlite"
    assert materialize_saved_input(store, saved, output=cold)["coverage_status"] == "partial"
    assert saved.descriptor["summary"]["coverage_status"] == "partial"
    assert rows(market) == rows(cold)
    assert [row[0] for row in rows(market)] == [last.isoformat()]
    with closing(sqlite3.connect(market)) as connection:
        assert uncovered_days(connection, "jquants_margin_alerts", (first, last)) == [first]
    [retained] = owner.fixed().requests(store, days=(first,))
    assert retained.request is not None
    assert retained.request.pages == raw.pages

    later = AT + timedelta(hours=1)
    owner.commit([attempt("corrected", day=first, at=later)])
    fixed = model_input(
        store, owner.fixed(), as_of=last, evaluated_at=later, market=market, mirror_root=mirror
    )
    assert fixed.descriptor["summary"]["coverage_status"] == "complete"
    assert [row[0] for row in rows(market)] == [first.isoformat(), last.isoformat()]


@pytest.mark.parametrize("warm_proof", [False, True], ids=["no-proof", "current-proof"])
def test_legacy_publication_key_does_not_outlive_the_projection(tmp_path, warm_proof):
    first, last = date(2025, 12, 31), date(2026, 1, 1)
    store = MemoryR2Store()
    owner = MarginAlertsL1(store, tmp_path / "native")
    owner.initialize()
    owner.commit([attempt("first", day=first), attempt("last", day=last)])
    market, mirror = tmp_path / "market.sqlite", tmp_path / "mirror"
    open_connection(market).close()
    fixed = owner.fixed()
    clean, previous = build(store, owner, market, mirror, fixed=fixed, as_of=last)
    assert previous.manifest.coverage_status == "complete"
    with closing(sqlite3.connect(market)) as connection, connection:
        # The raw legacy key is in range as text, but no year partition reads it.
        connection.execute(
            "INSERT INTO jquants_margin_alerts(publication_date,ticker,applied_date) "
            "VALUES ('20251231','9999','2025-12-31')"
        )

    store.get_keys.clear()
    saved, report = build(
        store,
        owner,
        market,
        mirror,
        fixed=fixed,
        as_of=last,
        previous=previous.manifest if warm_proof else None,
    )
    pack_reads = [key for key in store.get_keys if key.startswith(PREFIX + "packs/")]
    assert bool(pack_reads) is not warm_proof
    assert report.new_objects == 0
    assert saved == clean
    cold = tmp_path / "cold.sqlite"
    assert materialize_saved_input(store, saved, output=cold)["coverage_status"] == "complete"
    assert [row[0] for row in rows(market)] == [first.isoformat(), last.isoformat()]
    assert rows(market) == rows(cold)


def test_equal_recheck_uses_existing_l2_but_not_old_confirmation(tmp_path):
    store, owner, market, mirror = fixture(tmp_path, earlier=date(2026, 10, 1))
    prior_saved, first = build(store, owner, market, mirror)
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
    prior_selection = prior_saved.descriptor["selection_sha256"]
    assert store.get_keys.count(f"{INPUT_PREFIX}selections/{prior_selection}.json") == 1
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
    with closing(sqlite3.connect(market)) as conn:
        assert conn.execute(
            "SELECT publication_date, applied_date FROM jquants_margin_alerts "
            "ORDER BY publication_date, applied_date"
        ).fetchall() == [
            (DAY.isoformat(), prior.isoformat()),
            (DAY.isoformat(), DAY.isoformat()),
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


def test_warm_ranges_split_at_changed_input_and_keep_the_last_days_clock(tmp_path, monkeypatch):
    from baibai_jobs.integration import margin_alerts_native as projection

    start, changed = DAY - timedelta(days=6), DAY - timedelta(days=2)
    store, owner, market, mirror = fixture(tmp_path, earlier=start)
    _, previous = build(store, owner, market, mirror)
    owner.commit(
        [
            attempt("reconfirmed", day=start, at=AT + timedelta(hours=2)),
            attempt("changed", day=changed, at=AT + timedelta(hours=1), rows=records(changed)),
            attempt(
                "failed", day=DAY - timedelta(days=1), at=AT + timedelta(hours=1), partial=True
            ),
        ]
    )
    calls = []
    record = projection.record_range_source_coverage

    def record_range(connection, **kwargs):
        if kwargs["status"] == "ok":
            calls.append(
                (kwargs["requested_start"], kwargs["requested_end"], kwargs["fetched_at_utc"])
            )
        return record(connection, **kwargs)

    monkeypatch.setattr(projection, "record_range_source_coverage", record_range)
    saved, _ = build(
        store, owner, market, mirror, previous=previous.manifest, at=AT + timedelta(hours=3)
    )
    assert calls == [
        (start, changed - timedelta(days=1), AT.isoformat()),
        (changed + timedelta(days=1), DAY, AT.isoformat()),
    ]
    assert saved.descriptor["summary"]["partial_days"] == 1
    with closing(sqlite3.connect(market)) as conn:
        assert uncovered_days(
            conn, "jquants_margin_alerts", [changed, changed + timedelta(days=1), DAY]
        ) == [changed + timedelta(days=1)]


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


def test_normal_empty_is_complete_without_fabricating_a_lake_row(tmp_path):
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
    assert saved.descriptor["summary"]["coverage_status"] == "complete"


@pytest.mark.parametrize("boolean", [True, False])
def test_old_boolean_projection_is_rebuilt_before_warm_reuse(tmp_path, monkeypatch, boolean):
    from baibai_engine.machine_analysis.observations import jquants_writer
    from baibai_jobs.integration import margin_alerts_native as projection
    from baibai_jobs.publication import l1_margin_alerts_export as exporter

    store, owner, market, mirror = fixture(tmp_path)
    good = {**records()[0], "Code": "72030"}
    owner.commit(
        [attempt("boolean", at=AT + timedelta(hours=1), rows=[*records(long=boolean), good])]
    )
    current_float = jquants_writer.to_float
    with monkeypatch.context() as old:
        old.setattr(
            jquants_writer,
            "to_float",
            lambda value: float(value) if isinstance(value, bool) else current_float(value),
        )
        old.setattr(projection, "JQUANTS_NUMERIC_PROJECTION_REVISION", "jquants-numeric-v1")
        # Historical producer fixture: its Parquet footer had no numeric revision.
        old.setattr(exporter, "require_current_projection_revision", lambda _summary: None)
        before, previous = build(store, owner, market, mirror, at=AT + timedelta(hours=1))
    assert previous.manifest.coverage_status == "complete"
    assert len(rows(market)) == 2

    saved = model_input(
        store,
        owner.fixed(),
        as_of=DAY,
        evaluated_at=AT + timedelta(hours=1),
        market=market,
        mirror_root=mirror,
        previous=previous.manifest,
    )
    cold = tmp_path / "cold.sqlite"
    assert materialize_saved_input(store, saved, output=cold)["coverage_status"] == "partial"
    assert rows(market) == rows(cold)
    assert len(rows(market)) == 1
    assert saved.descriptor["summary"]["coverage_status"] == "partial"
    assert saved.descriptor["selection_sha256"] == before.descriptor["selection_sha256"]
    assert saved.sha256 != before.sha256


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
