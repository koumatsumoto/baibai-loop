"""旧原本の全期間・全stage移行と、原本不足を隠さない境界を検証する。"""

import gzip
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.objects import LakeObjectCache
from baibai_engine.storage.snapshots.writer import export_dataset_snapshot, sealed_sqlite_snapshot
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.maintenance import edinet_list_cutover as cutover

AT = datetime(2026, 11, 3, 1, 2, 3, 456789, tzinfo=UTC)
DAY = date(2026, 11, 2)


def row(*, identity=None, day=DAY, at=AT, body=b'{"results":[]}'):
    return (
        identity or uuid.uuid4().hex,
        day.isoformat(),
        at.isoformat(timespec="microseconds"),
        gzip.compress(body, mtime=123),
    )


def stage(tmp_path, store, source, rows, *, suffix="one", declared=None, status=None, at=AT):
    packet = tmp_path / (source + suffix)
    packet.mkdir()
    with closing(open_connection(packet / "receipts.sqlite")) as conn, conn:
        conn.executemany("INSERT INTO edinet_list_responses VALUES (?,?,?,?)", rows)
        conn.execute(
            "INSERT INTO edinet_source_archives VALUES ('other-original',1,?,?)",
            (DAY.isoformat(), b"keep exact archive bytes"),
        )
    report = {
        "source": source,
        "asof": DAY.isoformat(),
        "started_at": at.isoformat(),
        "list_receipts": [item[0] for item in rows] if declared is None else declared,
    }
    (packet / "result.json").write_text(json.dumps(report))
    stage_id = source + "-" + suffix
    save_handoff(store, stage_id, packet, market=True)
    if status:
        store.store_object(
            f"coordination/market-sources/{stage_id}/integrated.json",
            json.dumps({"status": status}).encode(),
        )
    return packet


def prepare(tmp_path, rows, *, projected=(), documents=(), store=None, configure=None):
    store = store or MemoryR2Store()
    database = tmp_path / "legacy.sqlite"
    with closing(open_connection(database)) as conn, conn:
        conn.executemany("INSERT INTO edinet_list_responses VALUES (?,?,?,?)", rows)
        conn.executemany(
            "INSERT INTO edinet_document_lists "
            "(doc_date,result_count,response_json,latest_received_at_utc) "
            "VALUES (?,0,'{}',?)",
            [(day, AT.isoformat()) for day in projected],
        )
        conn.executemany(
            "INSERT INTO edinet_documents(doc_date,sequence_number,doc_id) VALUES (?,1,'old-doc')",
            [(day,) for day in documents],
        )
    if configure is not None:
        configure(database)
    mirror = tmp_path / "lake"
    manifests = {}
    with sealed_sqlite_snapshot(sqlite_path=database, mirror_root=mirror) as snapshot:
        for dataset, present in (
            (cutover.EDINET_LIST_RESPONSES, rows),
            (cutover.EDINET_DOCUMENT_LISTS, projected),
            (cutover.EDINET_DOCUMENTS, documents),
        ):
            with closing(sqlite3.connect(database)) as conn:
                present = conn.execute(f"SELECT COUNT(*) FROM {dataset.sqlite_table}").fetchone()[0]
            if present:
                exported = export_dataset_snapshot(
                    dataset=dataset,
                    mirror_root=mirror,
                    producer_git_commit="a" * 40,
                    source_snapshot=snapshot,
                    build_id="test-" + dataset.name.replace(".", "-"),
                    created_at=AT + timedelta(days=1),
                )
                from baibai_engine.storage.snapshots.models import DatasetManifest

                manifests[dataset.name] = DatasetManifest.model_validate_json(
                    exported.manifest_path.read_bytes()
                )
    release = SimpleNamespace(
        release_id="fixed",
        manifest_sha256="a" * 64,
        dataset_names=lambda: tuple(manifests),
        dataset_manifest=lambda name: manifests[name],
    )
    cache = LakeObjectCache(
        tmp_path / "cache", SimpleNamespace(read_bytes=lambda key: (mirror / key).read_bytes())
    )
    before = list(store.put_keys)
    with lake_session() as session:
        plan = cutover.prepare_migration(
            session,
            release=release,
            cache=cache,
            store=store,
            known_at=AT + timedelta(days=2),
            directory=tmp_path / "plan",
        )
    assert store.put_keys == before
    return store, plan


def test_full_history_three_sources_late_recovery_and_exact_malformed_originals(tmp_path):
    store = MemoryR2Store()
    old = row(day=date(2019, 1, 1), at=AT.replace(microsecond=0), body=b"not json\xff")
    items = [old, row(body=b'{"correction":1}'), row(body=b'{"withdrawal":1}')]
    # Distinct IDs with identical bytes are distinct receipt events.
    items.append(row(body=b'{"withdrawal":1}'))
    stage(tmp_path, store, "edinet-list", [items[1]], suffix="integrated", status="integrated")
    stage(tmp_path, store, "edinet-list", [old], suffix="late", at=AT - timedelta(days=1))
    stage(tmp_path, store, "edinet-metrics", [items[2]], status="needs_explicit_recovery")
    stage(tmp_path, store, "edinet-facts", [items[3]])
    before = {key: value.body for key, value in store.values.items()}
    store, plan = prepare(tmp_path, [old], store=store)
    assert not plan["blocked"]
    assert not plan["missing_raw_days"]
    assert any(entry.get("late_before_integrated") for entry in plan["handoffs"])
    assert len(plan["receipts"]) == 4
    owner = EdinetListL1(store, tmp_path / "target")
    assert cutover.apply_migration(owner, directory=tmp_path / "plan")["added"] == 4
    actual = {item.receipt_id: item for item in owner.freeze().iter_receipts()}
    for identity, day, at, body in items:
        assert actual[identity] == NativeListReceipt(
            identity, date.fromisoformat(day), datetime.fromisoformat(at), body
        )
    cutover.verify_migration(EdinetListL1(store, tmp_path / "cold"), directory=tmp_path / "plan")
    assert cutover.apply_migration(owner, directory=tmp_path / "plan")["equal"] == 4
    assert all(store.values[key].body == body for key, body in before.items())


def test_normalized_only_historical_union_blocks_no_synthetic_original(tmp_path):
    store, plan = prepare(tmp_path, [row()], projected=("2010-01-01",), documents=("2011-01-01",))
    assert plan["missing_raw_days"] == ["2010-01-01", "2011-01-01"]
    before = list(store.put_keys)
    with pytest.raises(ValueError, match="missing_raw_days"):
        cutover.apply_migration(
            EdinetListL1(store, tmp_path / "target"), directory=tmp_path / "plan"
        )
    assert store.put_keys == before


@pytest.mark.parametrize("change", ["body", "day", "at"])
def test_same_id_metadata_or_byte_conflicts_block(tmp_path, change):
    old = row()
    modified = list(old)
    modified[{"body": 3, "day": 1, "at": 2}[change]] = {
        "body": gzip.compress(b"changed"),
        "day": "2020-01-01",
        "at": (AT - timedelta(seconds=1)).isoformat(timespec="microseconds"),
    }[change]
    store = MemoryR2Store()
    stage(tmp_path, store, "edinet-metrics", [modified])
    _, plan = prepare(tmp_path, [old], store=store)
    assert "same receipt ID" in plan["blocked"][0]["reason"]


@pytest.mark.parametrize("kind", ["missing", "invalid", "future"])
def test_declared_missing_invalid_manifest_and_future_block(tmp_path, kind):
    store = MemoryR2Store()
    if kind == "missing":
        stage(tmp_path, store, "edinet-facts", [], declared=[uuid.uuid4().hex])
    elif kind == "invalid":
        store.store_object("coordination/market-sources/edinet-list-bad/handoff.json", b"{}")
    _, plan = prepare(
        tmp_path, [row(at=AT + timedelta(days=3)) if kind == "future" else row()], store=store
    )
    assert plan["blocked"]
    with pytest.raises(ValueError, match="unresolved"):
        cutover.apply_migration(
            EdinetListL1(store, tmp_path / "target"), directory=tmp_path / "plan"
        )
    assert not store.put_keys or kind == "missing"


@pytest.mark.parametrize("artifact", ["body", "parquet", "sqlite", "report", "plan"])
def test_changed_prepared_input_fails_before_any_write(tmp_path, artifact):
    store = MemoryR2Store()
    stage(tmp_path, store, "edinet-list", [row()])
    store, plan = prepare(tmp_path, [row()], store=store)
    name = {
        "body": plan["receipts"][0]["file"],
        "parquet": plan["inputs"][0]["file"],
        "sqlite": "stage-00000/receipts.sqlite",
        "report": "stage-00000/result.json",
        "plan": "plan.json",
    }[artifact]
    (tmp_path / "plan" / name).write_bytes(b"changed")
    before = list(store.put_keys)
    with pytest.raises(ValueError, match="changed"):
        cutover.apply_migration(
            EdinetListL1(store, tmp_path / "target"), directory=tmp_path / "plan"
        )
    assert store.put_keys == before


def test_target_unrelated_ids_block_but_exact_partial_retry_succeeds(tmp_path):
    store, _plan = prepare(tmp_path, [row(), row()])
    owner = EdinetListL1(store, tmp_path / "target")
    _, receipts = cutover._prepared(tmp_path / "plan")
    owner.freeze(require_existing=False)
    owner.publish(receipts[:1])
    assert cutover.apply_migration(owner, directory=tmp_path / "plan")["added"] == 1
    owner.publish([NativeListReceipt(uuid.uuid4().hex, DAY, AT, gzip.compress(b"extra"))])
    before = list(store.put_keys)
    with pytest.raises(ValueError, match="outside this migration"):
        cutover.apply_migration(owner, directory=tmp_path / "plan")
    with pytest.raises(ValueError, match="outside this migration"):
        cutover.verify_migration(owner, directory=tmp_path / "plan")
    assert store.put_keys == before


def test_stage_normalized_only_day_cannot_shrink_migration_scope(tmp_path):
    store = MemoryR2Store()
    packet = stage(tmp_path, store, "edinet-facts", [])
    with sqlite3.connect(packet / "receipts.sqlite") as conn:
        conn.execute(
            "INSERT INTO edinet_documents(doc_date,sequence_number,doc_id) "
            "VALUES ('2008-01-01',1,'normalized-only')"
        )
    save_handoff(store, "edinet-facts-projection", packet, market=True)
    _, plan = prepare(tmp_path, [row()], store=store)
    assert plan["missing_raw_days"] == ["2008-01-01"]


def test_missing_raw_dataset_does_not_turn_normalized_projection_into_http_body(tmp_path):
    store, plan = prepare(tmp_path, [], projected=("2009-01-01",))
    assert not plan["receipts"]
    assert plan["missing_raw_days"] == ["2009-01-01"]
    with pytest.raises(ValueError, match="missing_raw_days"):
        cutover.apply_migration(
            EdinetListL1(store, tmp_path / "target"), directory=tmp_path / "plan"
        )
    assert not store.put_keys


def project_fixture(database):
    from baibai_engine.data_collection.providers.edinet.list_projection import project_document_list
    from baibai_engine.data_collection.providers.edinet.receipts import EdinetListReceipt
    from baibai_engine.machine_analysis.observations.disclosures.edinet_list_rebuild import (
        store_edinet_document_projection,
    )

    with closing(sqlite3.connect(database)) as conn:
        receipts = [
            EdinetListReceipt(
                str(a), date.fromisoformat(b), datetime.fromisoformat(c), gzip.decompress(d)
            )
            for a, b, c, d in conn.execute("SELECT * FROM edinet_list_responses")
        ]
    store_edinet_document_projection(database, project_document_list(receipts), rechecked_asof=None)


def valid_body(*, legal="1", sec="20000"):
    document = {"seqNumber": 1, "docID": "S100TEST", "legalStatus": legal}
    if sec is not None:
        document["secCode"] = sec
    return json.dumps(
        {
            "metadata": {"parameter": {"date": DAY.isoformat()}, "resultset": {"count": 1}},
            "results": [document],
        }
    ).encode()


def test_missing_selected_receipt_blocks_even_if_same_day_has_another_raw(tmp_path):
    original = row(body=valid_body())

    def configure(database):
        project_fixture(database)
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute(
                "UPDATE edinet_document_lists SET selected_receipt_id=?", (uuid.uuid4().hex,)
            )

    _, plan = prepare(tmp_path, [original], configure=configure)
    assert not plan["missing_raw_days"]
    assert any("selected receipt is missing" in item["reason"] for item in plan["blocked"])


def test_legacy_enriched_identity_without_earlier_raw_blocks(tmp_path):
    vanished = row(body=valid_body(legal="0", sec=None))

    def configure(database):
        project_fixture(database)
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute("UPDATE edinet_documents SET sec_code='20000'")

    _, plan = prepare(tmp_path, [vanished], configure=configure)
    assert any("identity needs unavailable" in item["reason"] for item in plan["blocked"])


def test_exact_selected_projection_can_migrate_before_later_stage(tmp_path):
    original = row(body=valid_body())
    changed = row(at=AT + timedelta(seconds=1), body=valid_body(legal="0", sec=None))
    store = MemoryR2Store()
    stage(tmp_path, store, "edinet-metrics", [changed])
    _, plan = prepare(tmp_path, [original], configure=project_fixture, store=store)
    assert not plan["blocked"]
    assert len(plan["receipts"]) == 2


def test_legacy_recheck_claim_requires_actual_current_day_raw(tmp_path):
    original = row(body=valid_body())

    def configure(database):
        project_fixture(database)
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute(
                "UPDATE edinet_document_lists SET rechecked_asof=?", (AT.date().isoformat(),)
            )

    _, plan = prepare(tmp_path, [original], configure=configure)
    assert any("real current-day proof" in item["reason"] for item in plan["blocked"])
