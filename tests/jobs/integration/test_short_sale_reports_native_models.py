"""Normal short-sale input preserves row order while reusing disposable projections."""

import sqlite3
from contextlib import closing
from datetime import timedelta

import pytest
from tests.helpers.master_calendar_native import (
    native_master_calendar_publication as native_master_calendar_publication,
)
from tests.helpers.price_native import native_price_publication as native_price_publication
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.short_sale_reports_native import AT, DAY, attempt, records

from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    HEAD_KEY,
    PREFIX,
    ShortSaleReportsL1,
)
from baibai_engine.machine_analysis.generation.short_sale_reports_inputs import (
    INPUT_PREFIX,
)
from baibai_engine.storage.snapshots.writer import sealed_sqlite_snapshot
from baibai_jobs.integration.short_sale_reports_native import (
    fixed_input,
    materialize_saved_input,
    model_input,
)
from baibai_jobs.publication.l1_short_sale_reports_export import (
    export_short_sale_reports_from_native_input,
)


def rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT * FROM jquants_short_sale_reports ORDER BY disclosed_at,source_ordinal"
        ).fetchall()


def coverage(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT * FROM source_coverage WHERE source='jquants_short_sale_reports' "
            "ORDER BY coverage_start"
        ).fetchall()


def fixture(tmp_path):
    store = MemoryR2Store()
    owner = ShortSaleReportsL1(store, tmp_path / "native")
    owner.initialize()
    first = DAY - timedelta(days=2)
    owner.commit(
        [
            attempt("october", day=first),
            attempt("empty", day=first + timedelta(days=1), rows=[]),
            attempt(
                "november",
                rows=[
                    *records(calculated=first, name="B"),
                    *records(calculated=first, name="A"),
                    *records(calculated=first, name="A"),
                ],
            ),
        ]
    )
    market, mirror = tmp_path / "market.sqlite", tmp_path / "mirror"
    saved, published = build(store, owner.fixed(), market, mirror)
    return store, owner, market, mirror, saved, published


def build(store, fixed, market, mirror, *, at=AT, previous=None):
    saved = model_input(
        store,
        fixed,
        as_of=DAY,
        evaluated_at=at,
        market=market,
        mirror_root=mirror,
        previous=previous,
    )
    with sealed_sqlite_snapshot(sqlite_path=market, mirror_root=mirror) as snapshot:
        exported = export_short_sale_reports_from_native_input(
            source_snapshot=snapshot,
            saved=saved,
            mirror_root=mirror,
            producer_git_commit="a" * 40,
        )
    for part in exported.manifest.partitions:
        for obj in part.objects:
            store.store_object(
                obj.key,
                (mirror / obj.key).read_bytes(),
                content_type="application/vnd.apache.parquet",
            )
    return saved, exported.manifest


def test_equal_recheck_keeps_order_duplicates_and_reuses_objects(tmp_path):
    store, owner, market, mirror, prior_saved, previous = fixture(tmp_path)
    expected_rows = rows(market)
    old_objects = {obj.key for part in previous.partitions for obj in part.objects}
    current_rows = [
        *records(calculated=DAY - timedelta(days=2), name="B"),
        *records(calculated=DAY - timedelta(days=2), name="A"),
        *records(calculated=DAY - timedelta(days=2), name="A"),
    ]
    owner.commit([attempt("equal", at=AT + timedelta(hours=1), rows=current_rows)])
    fixed = owner.fixed()
    store.get_keys.clear()
    saved, newer = build(
        store, fixed, market, mirror, at=AT + timedelta(hours=1), previous=previous
    )
    assert rows(market) == expected_rows
    assert not [key for key in store.get_keys if key.startswith(PREFIX + "packs/")]
    assert HEAD_KEY not in store.get_keys
    prior_selection = prior_saved.descriptor["selection_sha256"]
    assert store.get_keys.count(f"{INPUT_PREFIX}selections/{prior_selection}.json") == 1
    assert {obj.key for part in newer.partitions for obj in part.objects} == old_objects
    early = (DAY - timedelta(days=2)).isoformat()
    with closing(sqlite3.connect(market)) as conn:
        assert conn.execute(
            "SELECT source_ordinal, short_seller_name, calculated_at "
            "FROM jquants_short_sale_reports WHERE disclosed_at=? ORDER BY source_ordinal",
            (DAY.isoformat(),),
        ).fetchall() == [(0, "B", early), (1, "A", early), (2, "A", early)]
    cold = tmp_path / "cold.sqlite"
    materialize_saved_input(store, saved, output=cold)
    assert rows(cold) == rows(market)
    assert coverage(cold) == coverage(market)


def test_fixed_raw_short_sale_input_can_rebuild_with_current_code(tmp_path):
    store, owner, market, _mirror, _saved, _previous = fixture(tmp_path)
    saved = fixed_input(
        store, owner.fixed(), start=DAY - timedelta(days=2), end=DAY, evaluated_at=AT
    )
    assert "projection_revision" not in saved.descriptor["summary"]
    assert "coverage_status" not in saved.descriptor["summary"]
    cold = tmp_path / "raw.sqlite"
    assert materialize_saved_input(store, saved, output=cold)["input_sha256"] == saved.sha256
    assert rows(cold) == rows(market)


@pytest.mark.parametrize("boolean", [True, False])
def test_old_boolean_projection_is_rebuilt_once_then_reused(tmp_path, monkeypatch, boolean):
    from baibai_engine.machine_analysis.observations import jquants_writer
    from baibai_jobs.integration import short_sale_reports_native as projection
    from baibai_jobs.publication import l1_short_sale_reports_export as exporter

    store, owner, market, mirror, _, _ = fixture(tmp_path)
    owner.commit([attempt("boolean", at=AT + timedelta(hours=1), rows=records(ratio=boolean))])
    fixed = owner.fixed()
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
        before, previous = build(store, fixed, market, mirror, at=AT + timedelta(hours=1))
    with closing(sqlite3.connect(market)) as conn:
        assert conn.execute(
            "SELECT short_ratio FROM jquants_short_sale_reports WHERE disclosed_at=?",
            (DAY.isoformat(),),
        ).fetchall() == [(float(boolean),)]
    store.get_keys.clear()
    saved, current = build(
        store, fixed, market, mirror, at=AT + timedelta(hours=1), previous=previous
    )
    assert any(key.startswith(PREFIX + "packs/") for key in store.get_keys)
    cold = tmp_path / "cold.sqlite"
    materialize_saved_input(store, saved, output=cold)
    assert rows(market) == rows(cold)
    assert coverage(market) == coverage(cold)
    with closing(sqlite3.connect(market)) as conn:
        assert conn.execute(
            "SELECT short_ratio FROM jquants_short_sale_reports WHERE disclosed_at=?",
            (DAY.isoformat(),),
        ).fetchall() == [(None,)]
    assert saved.descriptor["selection_sha256"] == before.descriptor["selection_sha256"]
    assert saved.sha256 != before.sha256
    assert {source for part in current.partitions for source in part.sources} == {
        source for part in previous.partitions for source in part.sources
    }

    store.get_keys.clear()
    same, reused = build(store, fixed, market, mirror, at=AT + timedelta(hours=1), previous=current)
    assert same == saved
    assert not [key for key in store.get_keys if key.startswith(PREFIX + "packs/")]
    assert {obj for part in reused.partitions for obj in part.objects} == {
        obj for part in current.partitions for obj in part.objects
    }


def test_old_text_coercion_cache_rebuilds_same_fixed_report_without_changing_originals(
    tmp_path, monkeypatch
):
    import pyarrow.parquet as pq

    from baibai_engine.machine_analysis.observations import jquants_writer
    from baibai_engine.storage.snapshots.contracts import PROJECTION_REVISION_METADATA_KEY
    from baibai_jobs.integration import short_sale_reports_native as projection
    from baibai_jobs.publication import l1_short_sale_reports_export as publication

    store, owner, market, mirror, _, _ = fixture(tmp_path)
    at = AT + timedelta(hours=1)
    malformed = {**records(name="Malformed native Code")[0], "Code": True}
    primary = {**records(ratio=0)[0], "Code": "72030"}
    missing_ratio = {**records(ratio=True)[0], "Code": "83060"}
    secondary = {**records()[0], "Code": "72031"}
    owner.commit(
        [attempt("mixed-code-types", at=at, rows=[malformed, primary, missing_ratio, secondary])]
    )
    fixed = owner.fixed()
    code_quality = jquants_writer.code_quality
    with monkeypatch.context() as old:
        old.setattr(jquants_writer, "code_quality", lambda value, **_kwargs: code_quality(value))
        old.setattr(projection, "JQUANTS_NUMERIC_PROJECTION_REVISION", "jquants-numeric-v2")
        old.setattr(
            publication,
            "require_current_projection_revision",
            lambda _summary: "jquants-numeric-v2",
        )
        before, previous = build(store, fixed, market, mirror, at=at)
    assert previous.coverage_status == "complete"
    assert any(row[3] == "TRUE" for row in rows(market))
    previous_keys = {obj.key for part in previous.partitions for obj in part.objects}
    assert previous_keys
    assert all(
        pq.read_metadata(mirror / key).metadata[PROJECTION_REVISION_METADATA_KEY]
        == b"jquants-numeric-v2"
        for key in previous_keys
    )
    retained = {
        key: value.body
        for key, value in store.values.items()
        if key.startswith(PREFIX) or key in previous_keys
    }

    store.get_keys.clear()
    old_output = tmp_path / "explicit-v2.sqlite"
    with pytest.raises(ValueError, match="use the recorded producer commit"):
        materialize_saved_input(store, before, output=old_output)
    assert not old_output.exists()
    assert store.get_keys == []

    store.get_keys.clear()
    saved, current = build(store, fixed, market, mirror, at=at, previous=previous)
    raw_gets = [key for key in store.get_keys if key.startswith(PREFIX + "packs/")]
    assert raw_gets
    assert len(raw_gets) == len(set(raw_gets))
    assert HEAD_KEY not in store.get_keys
    cold = tmp_path / "cold-code.sqlite"
    assert materialize_saved_input(store, saved, output=cold)["input_sha256"] == saved.sha256
    assert rows(market) == rows(cold)
    assert coverage(market) == coverage(cold)
    assert [(row[1], row[3], row[7]) for row in rows(market) if row[0] == DAY.isoformat()] == [
        (0, "7203", 0.0),
        (1, "8306", None),
    ]
    assert current.coverage_status == "partial"
    current_keys = {obj.key for part in current.partitions for obj in part.objects}
    assert current_keys
    assert current_keys.isdisjoint(previous_keys)
    assert all(
        pq.read_metadata(mirror / key).metadata[PROJECTION_REVISION_METADATA_KEY]
        == projection.JQUANTS_NUMERIC_PROJECTION_REVISION.encode()
        for key in current_keys
    )
    assert saved.descriptor["selection_sha256"] == before.descriptor["selection_sha256"]
    assert saved.descriptor["format"] == before.descriptor["format"]
    assert saved.sha256 != before.sha256
    assert {key: store.values[key].body for key in retained} == retained
    assert {key for key in store.values if key.startswith(PREFIX)} == {
        key for key in retained if key.startswith(PREFIX)
    }


@pytest.mark.parametrize(
    "damage", ["value", "deleted", "extra", "ordinal", "database", "parquet", "partial"]
)
def test_disposable_cache_damage_returns_same_fixed_input(tmp_path, damage):
    store, owner, market, mirror, saved, previous = fixture(tmp_path)
    fixed, expected = owner.fixed(), rows(market)
    if damage == "database":
        market.unlink()
    elif damage in {"value", "deleted", "extra", "ordinal"}:
        with sqlite3.connect(market) as connection:
            if damage == "value":
                connection.execute("UPDATE jquants_short_sale_reports SET short_ratio=0.99")
            elif damage == "deleted":
                connection.execute("DELETE FROM jquants_short_sale_reports WHERE source_ordinal=1")
            elif damage == "extra":
                connection.execute(
                    "INSERT INTO jquants_short_sale_reports SELECT disclosed_at,99,calculated_at,ticker,"
                    "short_seller_name,discretionary_investment_contractor_name,investment_fund_name,"
                    "short_ratio,short_shares,short_trading_units,previous_reported_at,previous_short_ratio,"
                    "notes,short_seller_address,discretionary_investment_contractor_address "
                    "FROM jquants_short_sale_reports LIMIT 1"
                )
            else:
                connection.execute(
                    "UPDATE jquants_short_sale_reports SET source_ordinal=source_ordinal+10"
                )
    elif damage == "parquet":
        for part in previous.partitions:
            for obj in part.objects:
                (mirror / obj.key).unlink()
                del store.values[obj.key]
    else:
        previous = previous.model_copy(update={"coverage_status": "partial"})
    owner.commit([attempt("later", at=AT + timedelta(hours=1), rows=[])])
    del store.values[HEAD_KEY]
    store.get_keys.clear()
    rebuilt = model_input(
        store,
        fixed,
        as_of=DAY,
        evaluated_at=AT,
        market=market,
        mirror_root=mirror,
        previous=previous,
    )
    assert rebuilt == saved
    assert rows(market) == expected
    assert HEAD_KEY not in store.get_keys
    assert any(key.startswith(PREFIX + "packs/") for key in store.get_keys)


def test_reordering_is_changed_and_empty_replaces_only_discdate(tmp_path):
    store, owner, market, mirror, old_saved, previous = fixture(tmp_path)
    early = DAY - timedelta(days=2)
    owner.commit(
        [
            attempt(
                "reordered",
                at=AT + timedelta(hours=1),
                rows=[
                    *records(calculated=early, name="A"),
                    *records(calculated=early, name="B"),
                    *records(calculated=early, name="A"),
                ],
            )
        ]
    )
    saved, next_manifest = build(
        store, owner.fixed(), market, mirror, at=AT + timedelta(hours=1), previous=previous
    )
    with closing(sqlite3.connect(market)) as conn:
        assert conn.execute(
            "SELECT short_seller_name FROM jquants_short_sale_reports WHERE disclosed_at=? "
            "ORDER BY source_ordinal",
            (DAY.isoformat(),),
        ).fetchall() == [("A",), ("B",), ("A",)]
    assert saved != old_saved
    old_keys = {
        obj.key
        for part in previous.partitions
        for obj in part.objects
        if obj.min_key[0].startswith("2026-10")
    }
    new_keys = {
        obj.key
        for part in next_manifest.partitions
        for obj in part.objects
        if obj.min_key[0].startswith("2026-10")
    }
    assert new_keys == old_keys
    owner.commit([attempt("withdraw", at=AT + timedelta(hours=2), rows=[])])
    saved, manifest = build(
        store, owner.fixed(), market, mirror, at=AT + timedelta(hours=2), previous=next_manifest
    )
    assert manifest.coverage_status == "complete"
    with closing(sqlite3.connect(market)) as conn:
        assert conn.execute("SELECT disclosed_at FROM jquants_short_sale_reports").fetchall() == [
            (early.isoformat(),)
        ]
    assert materialize_saved_input(store, old_saved, output=tmp_path / "old.sqlite")["rows"] == 4


@pytest.mark.parametrize("disposition", ["empty", "failed"])
def test_normal_publication_and_paired_hydrate_keep_coverage(tmp_path, monkeypatch, disposition):
    from tests.helpers.lake_policy import narrow_release_policy

    from baibai_engine.storage.snapshots.duck import lake_session
    from baibai_engine.storage.snapshots.hydrate import dehydrate_market_store, hydrate_market_store
    from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
    from baibai_engine.storage.snapshots.reader import resolve_release
    from baibai_jobs.publication import snapshot_publication as publication
    from baibai_jobs.publication.datasets import LAKE_DATASETS

    store, owner, market, mirror, _, _ = fixture(tmp_path)
    name = "jquants.short_sale_reports"
    catalog = {name: LAKE_DATASETS[name]}
    narrow_release_policy(monkeypatch, datasets=(name,), carries_history=None)
    monkeypatch.setattr(publication, "LAKE_DATASETS", catalog)
    monkeypatch.setattr(publication, "lake_verified_git_commit", lambda: "b" * 40)
    owner.commit(
        [
            attempt(
                "new",
                at=AT + timedelta(hours=1),
                rows=[] if disposition == "empty" else None,
                partial=disposition == "failed",
            )
        ]
    )
    store.get_keys.clear()
    with publication.prepare_market_lake(
        sqlite_path=market, mirror_root=mirror, store=store, as_of=DAY
    ) as prepared:
        saved = prepared.short_sale_reports_identity
        assert saved is not None
        assert store.get_keys.count(HEAD_KEY) == 1
        manifest = prepared.export.datasets[name].manifest
        assert {ref.sha256 for part in manifest.partitions for ref in part.sources} == {
            saved.descriptor["selection_sha256"]
        }
        expected_rows, expected_coverage = rows(market), coverage(market)
        owner.commit(
            [attempt("after-freeze", at=AT + timedelta(hours=2), rows=records(name="later"))]
        )
        reads_before_publish = store.get_keys.count(HEAD_KEY)
        published = publication.publish_prepared_market_lake(prepared, store=store)
        assert rows(prepared.source_snapshot.path) == expected_rows
        assert store.get_keys.count(HEAD_KEY) == reads_before_publish
    release = resolve_release(
        LocalMirrorSource(mirror),
        published.release_id,
        manifest_sha256=published.release_manifest_sha256,
    )
    dehydrate_market_store(market, catalog=catalog, release=release)
    assert rows(market) == []
    assert coverage(market) == expected_coverage
    with lake_session() as session:
        hydrate_market_store(
            session,
            catalog=catalog,
            release=release,
            cache=LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror)),
            store=market,
            dataset_names=(name,),
        )
    assert rows(market) == expected_rows
    assert coverage(market) == expected_coverage
    with closing(sqlite3.connect(market)) as conn:
        assert conn.execute(
            "SELECT status, record_count FROM source_coverage "
            "WHERE source='jquants_short_sale_reports' AND coverage_start=?",
            (DAY.isoformat(),),
        ).fetchall() == [("ok", 0) if disposition == "empty" else ("partial", 3)]


def test_missing_changed_original_rolls_back_cache_and_day_coverage(tmp_path):
    store, owner, market, mirror, _, previous = fixture(tmp_path)
    expected_rows, expected_coverage = rows(market), coverage(market)
    old_packs = {key for key in store.values if key.startswith(PREFIX + "packs/")}
    owner.commit([attempt("changed", at=AT + timedelta(hours=1), rows=records(name="changed"))])
    fixed = owner.fixed()
    for key in set(store.values) - old_packs:
        if key.startswith(PREFIX + "packs/"):
            del store.values[key]
    from baibai_engine.storage.objects.r2 import LakePublishError

    with pytest.raises(LakePublishError, match="unavailable"):
        model_input(
            store,
            fixed,
            as_of=DAY,
            evaluated_at=AT + timedelta(hours=1),
            market=market,
            mirror_root=mirror,
            previous=previous,
        )
    assert rows(market) == expected_rows
    assert coverage(market) == expected_coverage


pytestmark = pytest.mark.usefixtures(
    "native_price_publication", "native_master_calendar_publication"
)
