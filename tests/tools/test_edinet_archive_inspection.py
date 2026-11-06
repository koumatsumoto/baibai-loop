"""The one-time preflight reads metadata, never original bodies or remote writes."""

import hashlib
import json
from datetime import date
from unittest.mock import Mock

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.maintenance.test_edinet_archive_import import AT, DAY, source, stage
from tools.diagnostics.edinet_archive_inspection import (
    inspect_prepared_capacity,
    inspect_remote_inputs,
)

from baibai_engine.data_collection.providers.edinet.archive_native import (
    HEAD_KEY as ARCHIVE_HEAD,
)
from baibai_engine.data_collection.providers.edinet.archive_native import (
    EdinetArchiveL1,
    NativeArchive,
)
from baibai_engine.data_collection.providers.edinet.facts_scope import FactsScope
from baibai_engine.data_collection.providers.edinet.list_native import HEAD_KEY as LIST_HEAD
from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.storage.objects.r2 import R2Error
from baibai_jobs.maintenance.edinet_archive_import import prepare_import


def fixture(tmp_path):
    database, release = source(tmp_path)
    store = MemoryR2Store()
    store.store_object("market.sqlite", database.read_bytes())
    archives = EdinetArchiveL1(store, tmp_path / "archive")
    archives.initialize()
    archives.publish(
        [NativeArchive.from_response("S100NATIVE", 1, EdinetResponse(AT, b"retained ZIP"))],
        scope=FactsScope(date(2025, 1, 1), frozenset({"S100MISSING"})),
    )
    lists = EdinetListL1(store, tmp_path / "lists")
    lists.freeze(False)
    lists.publish([NativeListReceipt.from_response(DAY, EdinetResponse(AT, b"list body"))])
    for group in ("edinet-list", "edinet-metrics", "edinet-facts"):
        stage(
            tmp_path,
            store,
            group + "-old",
            [("S100OLD", 1, DAY, b"old original")],
            status="integrated",
        )
    directory = tmp_path / "inspection"
    directory.mkdir()
    return store, release, directory


def test_inspection_is_metadata_only_and_includes_all_old_source_prefixes(tmp_path):
    store, release, directory = fixture(tmp_path)
    orphan = "coordination/market-sources/edinet-facts-unpublished/receipts-" + "a" * 64 + ".sqlite"
    store.store_object(orphan, b"never print or read this retained original")
    before = dict(store.values)
    store.get_keys.clear()
    store.put_file = Mock(side_effect=AssertionError("preflight must never write"))
    result = inspect_remote_inputs(store, release, directory=directory)
    assert result["release_id"] == release.release_id
    assert result["manifest_sha256"] == release.manifest_sha256
    assert (
        result["canonical"]["metadata_sha256"]
        == hashlib.sha256(store.values["market.sqlite"].body).hexdigest()
    )
    assert result["archive_dataset"]["rows"] == 1
    assert result["native_archives"]["archive_count"] == 1
    assert result["native_archives"]["scope_doc_count"] == 1
    assert result["native_lists"]["receipt_count"] == 1
    assert [item["source"] for item in result["source_stages"]] == [
        "edinet-list",
        "edinet-metrics",
        "edinet-facts",
    ]
    assert all(item["handoffs"] == 1 for item in result["source_stages"])
    assert result["source_stages"][-1]["receipts_without_handoff_directory"] == 1
    assert result["source_stages"][-1]["receipt_objects"] == 2
    assert not any(key.endswith((".zip", ".gz", ".sqlite")) for key in store.get_keys)
    assert list(directory.iterdir()) == []
    assert store.values == before
    store.put_file.assert_not_called()


@pytest.mark.parametrize(
    ("key", "field"), [(ARCHIVE_HEAD, "native_archives"), (LIST_HEAD, "native_lists")]
)
def test_wire_digest_is_not_silently_replaced_by_normalized_owner_identity(tmp_path, key, field):
    store, release, directory = fixture(tmp_path)
    before = inspect_remote_inputs(store, release, directory=directory)
    wire = json.dumps(json.loads(store.values[key].body), indent=2).encode()
    store.store_object(key, wire)
    after = inspect_remote_inputs(store, release, directory=directory)
    assert after[field]["identity"] == before[field]["identity"]
    assert after[field]["wire_sha256"] == hashlib.sha256(wire).hexdigest()
    assert after[field]["wire_sha256"] != after[field]["identity"]


def test_absent_archive_head_does_not_initialize_and_missing_digest_is_explicit(tmp_path):
    store, release, directory = fixture(tmp_path)
    del store.values[ARCHIVE_HEAD]
    store.values["market.sqlite"].metadata.clear()
    puts = list(store.put_keys)
    result = inspect_remote_inputs(store, release, directory=directory)
    assert result["native_archives"]["present"] is False
    assert result["native_archives"]["wire_sha256"] is None
    assert result["canonical"]["metadata_sha256"] is None
    assert store.put_keys == puts
    assert ARCHIVE_HEAD not in store.values


def test_access_failure_is_not_reported_as_an_empty_target(tmp_path, monkeypatch):
    store, release, directory = fixture(tmp_path)
    head = store.head

    def denied(key):
        if key == ARCHIVE_HEAD:
            raise R2Error("unavailable")
        return head(key)

    monkeypatch.setattr(store, "head", denied)
    with pytest.raises(R2Error):
        inspect_remote_inputs(store, release, directory=directory)


def test_disappeared_stage_changes_both_inventory_digests(tmp_path):
    store, release, directory = fixture(tmp_path)
    before = inspect_remote_inputs(store, release, directory=directory)
    for key in tuple(store.values):
        if key.startswith("coordination/market-sources/edinet-facts-"):
            del store.values[key]
    after = inspect_remote_inputs(store, release, directory=directory)
    assert after["handoff_inventory_sha256"] != before["handoff_inventory_sha256"]
    assert after["listed_inventory_sha256"] != before["listed_inventory_sha256"]


def test_prepared_capacity_uses_distinct_zip_bytes_not_compressed_partition_size(tmp_path):
    database, release = source(
        tmp_path,
        rows=[
            ("S100ONE", 1, DAY, b"a" * 10),
            ("S100TWO", 5, DAY, b"a" * 10),
            ("S100THREE", 1, DAY, b"b" * 5),
        ],
    )
    store = MemoryR2Store()
    directory = tmp_path / "import"
    plan = prepare_import(store, source=database, release=release, directory=directory)
    before = {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    result = inspect_prepared_capacity(plan, directory=directory)
    assert result["archive_identities"] == 3
    assert result["unique_native_bodies"] == 2
    assert result["unique_native_body_bytes"] == 15
    assert result["native_body_write_peak_bytes"] == 25
    assert result["canonical_local_bytes"] == database.stat().st_size
    assert result["prepared_local_bytes"] == sum(map(len, before.values()))
    assert {
        str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()
    } == before
    assert not store.put_keys
