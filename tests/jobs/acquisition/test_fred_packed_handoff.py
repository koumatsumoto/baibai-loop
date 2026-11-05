"""FRED handoff/inputの本文を日次に複写せず、固定原受領を再現する。"""

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.native_sources import fred_transport as wire
from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.handoff import (
    accept_rechecks,
    export_receipts,
)
from baibai_engine.data_collection.receipts.native_sources.records import (
    read_snapshots,
    store_snapshot,
)
from baibai_engine.data_collection.receipts.native_sources.store import (
    initialize_database,
    record_source_run,
)
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, R2Error
from baibai_jobs.acquisition.economic_staging import load_handoff, save_handoff
from baibai_jobs.acquisition.source_inputs import load_input
from baibai_jobs.integration.economic_sources import integrate
from baibai_jobs.integration.native_source_merge import merge_stores
from baibai_jobs.publication.lease import _save_handle, acquire

SOURCE = "fred-dfedtarl"
DAY = date(2020, 1, 1)
AT = datetime(2026, 11, 1, tzinfo=UTC)


def receive(path, rows, at):
    with closing(initialize_database(path)) as conn, conn:
        confirm_source_response(conn, source_id=SOURCE, fetched_at=at, records_by_day=rows)
        record_source_run(
            conn,
            source_id=SOURCE,
            start=DAY,
            end=DAY,
            started_at=at,
            status="ok",
            record_count=sum(map(len, rows.values())),
        )


def packet(tmp_path, source, at, name):
    target = tmp_path / name
    target.mkdir()
    export_receipts(source, target / "receipts.sqlite", source_ids=[SOURCE], since=at)
    (target / "result.json").write_text(
        json.dumps(
            {
                "group": "fred_csv",
                "sources": [{"source_id": SOURCE, "status": "ok"}],
                "started_at": at.isoformat(),
                "asof": str(DAY),
            }
        )
    )
    return target


def packs(store):
    return [key for key in store.values if key.startswith(wire.PREFIX) and "/packs/" in key]


def test_same_value_three_daily_cycles_add_no_payload_copies_and_cold_input_survives(tmp_path):
    store = MemoryR2Store()
    empty = tmp_path / "empty.sqlite"
    with closing(initialize_database(empty)):
        pass
    store.store_object("indicators.sqlite", empty.read_bytes())
    handle, _ = acquire(store, "daily")
    lease = tmp_path / "lease.json"
    _save_handle(lease, handle)
    source = tmp_path / "source.sqlite"
    rows = {
        DAY + timedelta(days=i): [
            {
                "observation_date": str(DAY + timedelta(days=i)),
                "DFEDTARL": "0",
                "unused": hashlib.sha256(str(i).encode()).hexdigest(),
            }
        ]
        for i in range(240)
    }
    measurements = []
    first_pointer = None
    for cycle in range(3):
        at = AT + timedelta(days=cycle)
        receive(source, rows, at)
        stage = packet(tmp_path, source, at, "packet-" + str(cycle))
        before = len(store.put_keys)
        save_handoff(store, "fred_csv-" + str(cycle), stage)
        report = integrate(
            store,
            tmp_path / ("run-" + str(cycle)),
            database=tmp_path / ("run-" + str(cycle) + ".sqlite"),
            asof=DAY,
            lease_handle=lease,
        )
        assert len(report["inputs_updated"]) == 15
        assert report["handoffs"][0]["applied"]
        assert not report["handoffs"][0]["failed"]
        keys = packs(store)
        measurements.append(
            {
                "cycle": cycle + 1,
                "body_packs": len(keys),
                "body_bytes": sum(len(store.values[key].body) for key in keys),
                "immutable_bytes": sum(
                    len(value.body)
                    for key, value in store.values.items()
                    if key.startswith(wire.PREFIX) and not key.endswith("inventory.json")
                ),
                "new_body_puts": len([key for key in store.put_keys[before:] if "/packs/" in key]),
            }
        )
        pointer_key = "coordination/source-inputs/economic-fred_csv.json"
        if first_pointer is None:
            first_pointer = store.get_bytes(pointer_key)
    assert [row["new_body_puts"] for row in measurements] == [1, 0, 0]
    assert len({row["body_bytes"] for row in measurements}) == 1
    assert not any(
        key.endswith(".sqlite") and ("/fred_csv-" in key or "/economic-fred_csv-" in key)
        for key in store.values
    )
    # A pinned old input has no dependency on canonical, current pointer, or inventory.
    for key in list(store.values):
        if key == "indicators.sqlite" or key.endswith("inventory.json"):
            del store.values[key]
    store.store_object(pointer_key, first_pointer)
    store.get_keys.clear()
    downloaded = tmp_path / "cold.sqlite"
    load_input(store, "economic-fred_csv", downloaded, tmp_path / "cold")
    assert wire.snapshot(downloaded) == wire.snapshot(tmp_path / "run-0.sqlite")
    assert not any(
        key.endswith("inventory.json") or key == "indicators.sqlite" for key in store.get_keys
    )
    body_gets = [key for key in store.get_keys if "/packs/" in key]
    assert len(body_gets) == len(set(body_gets)) == 1
    print("FRED_PACK_MEASUREMENT", json.dumps(measurements), "COLD_GETS", len(store.get_keys))


def test_changed_old_period_and_a_b_a_reuse_payload_and_keep_receipt_clocks(tmp_path):
    store = MemoryR2Store()
    local, canonical = tmp_path / "local.sqlite", tmp_path / "canonical.sqlite"
    old, recent = DAY, date(2026, 11, 1)

    def rows(value):
        return {old: [{"DFEDTARL": value}], recent: [{"DFEDTARL": "1"}]}

    receive(local, rows("0"), AT)
    shutil.copyfile(local, canonical)
    first = packet(tmp_path, local, AT, "first")
    save_handoff(store, "fred_csv-first", first)
    receive(canonical, rows("."), AT + timedelta(hours=1))
    second = packet(tmp_path, canonical, AT + timedelta(hours=1), "second")
    save_handoff(store, "fred_csv-second", second)
    receive(local, rows("0"), AT + timedelta(hours=2))
    third = packet(tmp_path, local, AT + timedelta(hours=2), "third")
    before = list(packs(store))
    save_handoff(store, "fred_csv-third", third)
    assert packs(store) == before
    load_handoff(store, "fred_csv-third", tmp_path / "downloaded")
    accepted = tmp_path / "accepted.sqlite"
    outcome = accept_rechecks(tmp_path / "downloaded/receipts.sqlite", canonical, accepted)
    assert outcome["late_recovery_required"] == []
    merge_stores(accepted, canonical)
    with closing(sqlite3.connect(canonical)) as conn:
        actual = next(read_snapshots(conn, old, old, source_id=SOURCE))
        assert actual.records == ({"DFEDTARL": "0"},)
        assert actual.fetched_at == AT + timedelta(hours=2)
        assert (
            conn.execute(
                "SELECT count(*) FROM economic_source_records WHERE observed_at=?", (str(old),)
            ).fetchone()[0]
            == 3
        )


@pytest.mark.parametrize("point", ["pack", "inventory", "metadata", "root", "handoff"])
def test_unknown_ack_is_confirmed_by_same_bytes_before_manifest(tmp_path, monkeypatch, point):
    store = MemoryR2Store()
    source = tmp_path / "source.sqlite"
    receive(source, {DAY: [{"DFEDTARL": "0"}]}, AT)
    stage = packet(tmp_path, source, AT, "stage")
    put = store.put_file

    def uncertain(key, *args, **kwargs):
        result = put(key, *args, **kwargs)
        if (
            (point == "pack" and "/packs/" in key)
            or (point == "inventory" and key.endswith("/inventory.json"))
            or (point == "metadata" and "/metadata-packs/" in key)
            or (point == "root" and "/roots/" in key)
            or (point == "handoff" and key.endswith("/handoff.json"))
        ):
            raise R2Error("lost acknowledgement")
        return result

    monkeypatch.setattr(store, "put_file", uncertain)
    save_handoff(store, "fred_csv-one", stage)
    load_handoff(store, "fred_csv-one", tmp_path / "downloaded")
    assert wire.snapshot(tmp_path / "downloaded/receipts.sqlite") == wire.snapshot(
        stage / "receipts.sqlite"
    )


@pytest.mark.parametrize("point", ["pack", "inventory", "metadata", "root", "handoff"])
def test_unconfirmed_object_does_not_publish_handoff_and_fresh_retry_works(
    tmp_path, monkeypatch, point
):
    store = MemoryR2Store()
    source = tmp_path / "source.sqlite"
    receive(source, {DAY: [{"DFEDTARL": "0"}]}, AT)
    stage = packet(tmp_path, source, AT, "stage")
    put = store.put_file

    def failed(key, *args, **kwargs):
        if (
            (point == "pack" and "/packs/" in key)
            or (point == "inventory" and key.endswith("/inventory.json"))
            or (point == "metadata" and "/metadata-packs/" in key)
            or (point == "root" and "/roots/" in key)
            or (point == "handoff" and key.endswith("/handoff.json"))
        ):
            raise R2Error("not written")
        return put(key, *args, **kwargs)

    monkeypatch.setattr(store, "put_file", failed)
    with pytest.raises(R2Error):
        save_handoff(store, "fred_csv-one", stage)
    assert not any(key.endswith("/handoff.json") for key in store.values)
    monkeypatch.setattr(store, "put_file", put)
    save_handoff(store, "fred_csv-one", stage)
    load_handoff(store, "fred_csv-one", tmp_path / "downloaded")
    assert wire.snapshot(tmp_path / "downloaded/receipts.sqlite") == wire.snapshot(
        stage / "receipts.sqlite"
    )


def test_inventory_cas_union_does_not_overwrite_another_writer(tmp_path, monkeypatch):
    store = MemoryR2Store()
    source = tmp_path / "source.sqlite"
    receive(source, {DAY: [{"DFEDTARL": "0"}]}, AT)
    stage = packet(tmp_path, source, AT, "stage")
    put = store.put_file
    raced = False
    other = {"a" * 64: "b" * 64}

    def race(key, *args, **kwargs):
        nonlocal raced
        if key.endswith("/inventory.json") and not raced:
            raced = True
            store.store_object(key, json.dumps(other).encode())
            raise LakeCASConflict("another source writer")
        return put(key, *args, **kwargs)

    monkeypatch.setattr(store, "put_file", race)
    save_handoff(store, "fred_csv-one", stage)
    inventory = json.loads(store.get_bytes(wire.PREFIX + SOURCE + "/inventory.json"))
    assert inventory["a" * 64] == "b" * 64
    assert len(inventory) == 2
    load_handoff(store, "fred_csv-one", tmp_path / "read")


def test_missing_or_corrupt_fixed_pack_fails_without_canonical_fallback(tmp_path):
    store = MemoryR2Store()
    source = tmp_path / "source.sqlite"
    receive(source, {DAY: [{"DFEDTARL": "0"}]}, AT)
    stage = packet(tmp_path, source, AT, "stage")
    save_handoff(store, "fred_csv-one", stage)
    key = packs(store)[0]
    saved = store.values.pop(key)
    with pytest.raises(LakePublishError, match="missing"):
        load_handoff(store, "fred_csv-one", tmp_path / "missing")
    assert not (tmp_path / "missing/receipts.sqlite").exists()
    store.values[key] = saved
    store.store_object(key, b"{}")
    with pytest.raises(LakePublishError, match="changed"):
        load_handoff(store, "fred_csv-one", tmp_path / "corrupt")


def test_non_fred_source_cannot_enter_fred_transport(tmp_path):
    source = tmp_path / "bad.sqlite"
    with closing(initialize_database(source)) as conn, conn:
        store_snapshot(
            conn, source_id="boj-cai", observed_at=DAY, fetched_at=AT, records=[{"value": "x"}]
        )
    with pytest.raises(ValueError, match="another source"):
        wire.snapshot(source)


def test_same_stage_retry_verifies_fixed_originals_without_new_puts(tmp_path):
    store = MemoryR2Store()
    source = tmp_path / "source.sqlite"
    receive(source, {DAY: [{"DFEDTARL": "0"}]}, AT)
    stage = packet(tmp_path, source, AT, "stage")
    save_handoff(store, "fred_csv-one", stage)
    before = list(store.put_keys)
    assert save_handoff(store, "fred_csv-one", stage) == "fred_csv-one"
    assert store.put_keys == before
    del store.values[packs(store)[0]]
    with pytest.raises(LakePublishError, match="missing"):
        save_handoff(store, "fred_csv-one", stage)
    assert store.put_keys == before


def test_legacy_sqlite_handoff_remains_readable_without_rewriting_original(tmp_path):
    from baibai_engine.storage.snapshots.sources import sha256_file

    store = MemoryR2Store()
    source = tmp_path / "source.sqlite"
    receive(source, {DAY: [{"DFEDTARL": "0"}]}, AT)
    stage = packet(tmp_path, source, AT, "stage")
    path = stage / "receipts.sqlite"
    digest = sha256_file(path)
    key = "coordination/economic-sources/old/receipts-" + digest + ".sqlite"
    store.store_object(key, path.read_bytes())
    store.store_object(
        "coordination/economic-sources/old/handoff.json",
        json.dumps(
            {
                "result": json.loads((stage / "result.json").read_text()),
                "receipt_key": key,
                "receipt_sha256": digest,
                "receipt_bytes": path.stat().st_size,
            }
        ).encode(),
    )
    before = {key: value.body for key, value in store.values.items()}
    load_handoff(store, "old", tmp_path / "legacy")
    assert wire.snapshot(tmp_path / "legacy/receipts.sqlite") == wire.snapshot(path)
    assert save_handoff(store, "old", stage) == "old"
    assert {key: value.body for key, value in store.values.items()} == before


def test_fixed_receipt_does_not_depend_on_current_source_catalog(tmp_path, monkeypatch):
    store = MemoryR2Store()
    source = tmp_path / "source.sqlite"
    receive(source, {DAY: [{"DFEDTARL": "0"}]}, AT)
    stage = packet(tmp_path, source, AT, "stage")
    save_handoff(store, "fred_csv-one", stage)
    original = wire.snapshot(stage / "receipts.sqlite")
    monkeypatch.setattr(wire, "load_sources", dict)
    load_handoff(store, "fred_csv-one", tmp_path / "old-source")
    with closing(sqlite3.connect(tmp_path / "old-source/receipts.sqlite")) as conn:
        row = conn.execute(
            "SELECT source_id,observed_at,fetched_at,record_count FROM economic_source_records"
        ).fetchone()
        assert list(row) == original["economic_source_records"][0][:4]


def test_all_fifteen_groups_preserve_scope_and_source_specific_formats(tmp_path):
    from baibai_engine.data_collection.acquisition.policy import acquisition_groups, source_policy
    from baibai_engine.data_collection.catalog import DOCUMENT_PROVIDERS, load_sources
    from baibai_engine.data_collection.receipts.native_sources.documents import (
        PMI_INPUT_TABLE,
        SourceDocument,
        read_document_metadata,
        store_document,
    )
    from baibai_engine.data_collection.receipts.native_sources.umich_input import (
        TABLE as UMICH_TABLE,
    )

    store = MemoryR2Store()
    initial = tmp_path / "initial.sqlite"
    with closing(initialize_database(initial)):
        pass
    store.store_object("indicators.sqlite", initial.read_bytes())
    handle, _ = acquire(store, "daily")
    lease = tmp_path / "lease.json"
    _save_handle(lease, handle)
    selected = {}
    for group in acquisition_groups():
        source = next(
            value
            for value in load_sources().values()
            if source_policy(value).acquisition_group == group
        )
        selected[group] = source.source_id
        directory = tmp_path / group
        directory.mkdir()
        with closing(initialize_database(directory / "receipts.sqlite")) as conn, conn:
            if group == "fred_csv":
                confirm_source_response(
                    conn,
                    source_id=source.source_id,
                    fetched_at=AT,
                    records_by_day={DAY: [{"native": group}]},
                )
            elif source.provider in DOCUMENT_PROVIDERS:
                store_document(
                    conn,
                    SourceDocument(
                        source.source_id,
                        "one",
                        source.source_url,
                        "application/pdf",
                        AT,
                        b"provider-original",
                        {"observed_at": str(DAY)},
                    ),
                )
            else:
                store_snapshot(
                    conn,
                    source_id=source.source_id,
                    observed_at=DAY,
                    fetched_at=AT,
                    records=[{"native": group}],
                )
            record_source_run(
                conn,
                source_id=source.source_id,
                start=DAY,
                end=DAY,
                started_at=AT,
                status="ok",
                record_count=1,
            )
        (directory / "result.json").write_text(
            json.dumps(
                {
                    "group": group,
                    "sources": [{"source_id": source.source_id, "status": "ok"}],
                    "started_at": AT.isoformat(),
                    "asof": str(DAY),
                }
            )
        )
        save_handoff(store, group + "-one", directory)
    report = integrate(
        store,
        tmp_path / "integration",
        database=tmp_path / "canonical.sqlite",
        asof=DAY,
        lease_handle=lease,
    )
    assert len(report["inputs_updated"]) == 15
    assert all(row["applied"] and not row["failed"] for row in report["handoffs"])
    fixed = {}
    for group, source_id in selected.items():
        scoped_group = "economic-" + group
        pointer = json.loads(store.values[f"coordination/source-inputs/{scoped_group}.json"].body)
        fixed[group] = pointer
        manifest = json.loads(
            store.values[f"coordination/source-inputs/{pointer['stage_id']}/handoff.json"].body
        )
        if group == "fred_csv":
            assert manifest["format"] == wire.FORMAT
            assert "receipt_key" not in manifest
            assert "planning_key" not in manifest
        elif group in {"spglobal_pmi", "umich_sca"}:
            expected_format = (
                "pmi-acquisition-input-v1"
                if group == "spglobal_pmi"
                else "umich-acquisition-input-v1"
            )
            assert manifest["receipt_format"] == expected_format
            assert "planning_key" in manifest
            assert "receipt_key" not in manifest
            assert "format" not in manifest
        else:
            assert "receipt_key" in manifest
            assert "planning_key" not in manifest
            assert "format" not in manifest
        target = tmp_path / (group + "-loaded.sqlite")
        load_input(store, scoped_group, target, tmp_path / (group + "-loaded"))
        with (
            closing(sqlite3.connect(target)) as conn,
            closing(sqlite3.connect(tmp_path / "canonical.sqlite")) as canonical,
        ):
            present = set()
            for table in (
                "economic_source_records",
                "economic_source_documents",
                "economic_source_runs",
                "economic_source_record_confirmations",
                "economic_source_collection_confirmations",
            ):
                present.update(
                    row[0] for row in conn.execute("SELECT DISTINCT source_id FROM " + table)
                )
                actual = sorted(tuple(row) for row in conn.execute("SELECT * FROM " + table))
                expected = sorted(
                    tuple(row)
                    for row in canonical.execute(
                        "SELECT * FROM " + table + " WHERE source_id=?", (source_id,)
                    )
                )
                if (
                    group == "matsui_margin"
                    or (
                        group in {"tsr_bankruptcies", "spglobal_pmi"}
                        and table == "economic_source_documents"
                    )
                    or (
                        group == "umich_sca"
                        and table in {"economic_source_records", "economic_source_documents"}
                    )
                ):
                    assert actual == []
                else:
                    assert actual == expected
                if group in {"matsui_margin", "tsr_bankruptcies", "spglobal_pmi"} and (
                    table == "economic_source_documents"
                ):
                    assert expected  # The canonical original was not removed.
                if group == "umich_sca" and table == "economic_source_records":
                    assert expected  # The canonical native original was not removed.
            assert present == (set() if group == "matsui_margin" else {source_id})
            has_umich_planning = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (UMICH_TABLE,)
            ).fetchone()
            assert bool(has_umich_planning) == (group == "umich_sca")
            if has_umich_planning:
                assert list(
                    conn.execute(f"SELECT observed_at FROM {UMICH_TABLE} ORDER BY observed_at")
                ) == list(
                    canonical.execute(
                        "SELECT DISTINCT observed_at FROM economic_source_records WHERE source_id=? ORDER BY observed_at",
                        (source_id,),
                    )
                )
            has_planning = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (PMI_INPUT_TABLE,)
            ).fetchone()
            assert bool(has_planning) == (group == "spglobal_pmi")
            if has_planning:
                assert list(read_document_metadata(conn, source_id)) == list(
                    read_document_metadata(canonical, source_id)
                )
    first_packs = packs(store)
    # Even without a newly acquired FRED/PMI/UMich stage, the ordinary writer reissues
    # every input. Reader-first cannot be achieved by pausing just their providers.
    again = integrate(
        store,
        tmp_path / "integration-again",
        database=tmp_path / "canonical-again.sqlite",
        asof=DAY,
        lease_handle=lease,
    )
    assert len(again["inputs_updated"]) == 15
    assert packs(store) == first_packs
    for group, pointer in fixed.items():
        directory = tmp_path / (group + "-fixed")
        load_handoff(
            store,
            pointer["stage_id"],
            directory,
            namespace="source-inputs",
            input_group="economic-" + group,
        )
        if group == "fred_csv":
            assert wire.snapshot(directory / "receipts.sqlite") == wire.snapshot(
                tmp_path / (group + "-loaded.sqlite")
            )
        else:
            assert (directory / "receipts.sqlite").read_bytes() == (
                tmp_path / (group + "-loaded.sqlite")
            ).read_bytes()
    accessed = store.get_keys + store.head_keys + store.put_keys
    assert not any(
        key.startswith("lake/") or key in {"market.sqlite", "runs.sqlite", "machine-manifest.json"}
        for key in accessed
    )
