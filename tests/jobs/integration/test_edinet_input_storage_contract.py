"""保存EDINET pairの独立契約。原本取得・L2再生成・本番接続は行わない。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from tests.helpers.r2_store import MemoryR2Store

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
from baibai_engine.machine_analysis.generation.contracts import InputSnapshot
from baibai_engine.machine_analysis.generation.edinet_inputs import (
    read_edinet_input,
    retain_edinet_input,
)
from baibai_engine.storage.objects.r2 import LakePublishError, R2Error

PREFIX = "fixed-inputs/edinet/v1/"
AT = datetime(2026, 11, 1, 1, tzinfo=UTC)
SELECTED = AT + timedelta(hours=2)
DAY = AT.date()


def source_fixture(tmp_path, *, store=None, legacy_day=DAY):
    """別clock/不正list原文/原本未着scopeを持つ正常な固定metadataを作る。"""
    if store is None:
        store = MemoryR2Store()
    list_owner = EdinetListL1(store, tmp_path / "lists")
    list_owner.freeze(require_existing=False)
    lists = list_owner.publish(
        [
            NativeListReceipt.from_response(
                DAY,
                EdinetResponse(AT, b'{"metadata":{"resultset":{"count":0}},"results":[]}'),
                UUID(int=1).hex,
            ),
            NativeListReceipt.from_response(
                DAY, EdinetResponse(AT + timedelta(minutes=1), b"{"), UUID(int=2).hex
            ),
        ]
    )
    archive_owner = EdinetArchiveL1(store, tmp_path / "archives")
    archive_owner.initialize()
    legacy = NativeArchive.from_response(
        "S100OLD", 1, EdinetResponse(AT, b"fixture-edinet-legacy-zip")
    )
    legacy = NativeArchive(
        legacy.metadata.model_copy(update={"received_at_utc": None, "stored_on": legacy_day}),
        legacy.content,
    )
    current = NativeArchive.from_response(
        "S100NEW", 5, EdinetResponse(AT + timedelta(minutes=5), b"fixture-edinet-current-zip")
    )
    archives = archive_owner.publish(
        (legacy, current),
        scope=FactsScope(
            date(2025, 1, 1),
            frozenset({"S100OLD", "S100MISSING"}),
            frozenset({"a" * 64}),
        ),
    )
    clear_trace(store)
    return store, list_owner, archive_owner, lists, archives


def clear_trace(store):
    store.get_keys.clear()
    store.head_keys.clear()
    store.put_keys.clear()


def save(store, lists, archives, *, selected_at=SELECTED):
    return retain_edinet_input(store, lists, archives, selected_at=selected_at)


def assert_metadata_only(store):
    assert all(key.startswith(PREFIX) for key in store.get_keys)
    assert LIST_HEAD not in store.head_keys
    assert ARCHIVE_HEAD not in store.head_keys
    assert all(key.startswith(PREFIX) for key in store.put_keys)


def assert_same_pair(result, saved, lists, archives, selected_at=SELECTED):
    assert result.identity == saved
    assert result.lists.identity == lists.identity
    assert result.lists.value() == lists.value()
    assert result.archives.identity == archives.identity
    assert result.archives.value() == archives.value()
    assert result.archives.scope == archives.scope
    assert result.selected_at == selected_at


def test_roundtrip_retains_exact_pair_scope_and_clocks_without_source_body_io(tmp_path):
    store, _, _, lists, archives = source_fixture(tmp_path)
    original = dict(store.values)
    saved = save(store, lists, archives)
    assert saved.kind == "native_l1"
    assert saved.descriptor is not None
    assert set(saved.descriptor) == {"format", "selection_sha256"}
    assert saved.descriptor["format"] == "edinet-input-v1"
    assert len(saved.model_dump_json()) < 1000  # compact locator, not inline head history
    assert InputSnapshot.model_validate_json(saved.model_dump_json()) == saved
    assert_metadata_only(store)
    result = read_edinet_input(store, saved)
    assert_same_pair(result, saved, lists, archives)
    assert result.archives.metadata("S100OLD", 1).received_at_utc is None
    assert result.archives.metadata("S100NEW", 5).received_at_utc == AT + timedelta(minutes=5)
    assert ("S100MISSING", 1) not in result.archives.archive_keys
    assert result.archives.scope.doc_ids == frozenset({"S100OLD", "S100MISSING"})
    # Preserving malformed/partial receipt metadata is not declaring L2 success.
    assert len(result.lists.value()["receipts"]) == 2
    assert all(store.values[key] == value for key, value in original.items())
    assert all(
        b"fixture-edinet-" not in value.body
        for key, value in store.values.items()
        if key.startswith(PREFIX)
    )
    assert_metadata_only(store)


def test_identical_save_is_idempotent_and_only_changed_side_gets_new_metadata(tmp_path):
    store, _, owner, lists, archives = source_fixture(tmp_path)
    first = save(store, lists, archives)
    clear_trace(store)
    assert save(store, lists, archives) == first
    assert store.put_keys == []
    changed = owner.publish(
        [NativeArchive.from_response("S100LATER", 1, EdinetResponse(SELECTED, b"later"))]
    )
    clear_trace(store)
    later = save(store, lists, changed)
    assert later != first
    assert not any(key.startswith(PREFIX + "lists/") for key in store.put_keys)
    assert any(key.startswith(PREFIX + "archives/") for key in store.put_keys)
    assert any(key.startswith(PREFIX + "selections/") for key in store.put_keys)
    assert_same_pair(read_edinet_input(store, first), first, lists, archives)
    assert_same_pair(read_edinet_input(store, later), later, lists, changed)
    assert_metadata_only(store)


def test_saved_pair_survives_mutable_head_changes_and_removal(tmp_path):
    store, list_owner, archive_owner, lists, archives = source_fixture(tmp_path)
    saved = save(store, lists, archives)
    list_owner.publish(
        [NativeListReceipt.from_response(DAY, EdinetResponse(SELECTED, b"later"), UUID(int=3).hex)]
    )
    archive_owner.publish(
        [NativeArchive.from_response("S100LATER", 1, EdinetResponse(SELECTED, b"later"))]
    )
    del store.values[LIST_HEAD]
    del store.values[ARCHIVE_HEAD]
    clear_trace(store)
    assert_same_pair(read_edinet_input(store, saved), saved, lists, archives)
    assert_metadata_only(store)
    assert store.put_keys == []


def test_source_bodies_are_not_a_metadata_retention_precondition(tmp_path):
    store, _, _, lists, archives = source_fixture(tmp_path)
    for key in tuple(store.values):
        if key.endswith((".zip", ".body.gz")):
            del store.values[key]
    saved = save(store, lists, archives)
    result = read_edinet_input(store, saved)
    assert_same_pair(result, saved, lists, archives)
    assert_metadata_only(store)
    # A later actual consumer must distinguish a lost retained body from missing acquisition.
    with pytest.raises(LakePublishError):
        result.archives.read_archive("S100OLD", 1)


@pytest.mark.parametrize("kind", ["lists", "archives", "selections"])
@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_changed_fixed_metadata_fails_without_current_head_fallback(
    tmp_path, kind, missing
):
    store, _, _, lists, archives = source_fixture(tmp_path)
    saved = save(store, lists, archives)
    key = next(key for key in store.values if key.startswith(PREFIX + kind + "/"))
    if missing:
        del store.values[key]
    else:
        store.store_object(key, b"{}")
    clear_trace(store)
    with pytest.raises(LakePublishError):
        read_edinet_input(store, saved)
    assert_metadata_only(store)
    assert store.put_keys == []


def test_tampered_descriptor_is_revalidated_even_for_a_constructed_model(tmp_path):
    store, _, _, lists, archives = source_fixture(tmp_path)
    saved = save(store, lists, archives)
    changed = saved.model_copy(
        update={"descriptor": {"format": "edinet-input-v1", "selection_sha256": "f" * 64}}
    )
    clear_trace(store)
    with pytest.raises(ValueError, match="digest-matching fixed descriptor"):
        read_edinet_input(store, changed)
    assert store.get_keys == []
    assert store.put_keys == []


def test_legacy_market_snapshot_is_not_silently_resolved_from_current_native_heads(tmp_path):
    store, _, _, _, _ = source_fixture(tmp_path)
    legacy = InputSnapshot(kind="lake_release", reference="old-market", sha256="a" * 64)
    with pytest.raises(ValueError, match="native EDINET input"):
        read_edinet_input(store, legacy)
    assert store.get_keys == []
    assert store.put_keys == []


@pytest.mark.parametrize("selected_at", [AT.replace(tzinfo=None), AT, AT + timedelta(minutes=2)])
def test_invalid_or_pre_receipt_clock_is_rejected_before_any_retention(tmp_path, selected_at):
    store, _, _, lists, archives = source_fixture(tmp_path)
    with pytest.raises(ValueError, match=r"timezone|receipt is later than"):
        save(store, lists, archives, selected_at=selected_at)
    assert store.put_keys == []
    assert_metadata_only(store)


def test_legacy_receipt_day_uses_utc_not_local_selected_day(tmp_path):
    local = timezone(timedelta(hours=9))
    selected = datetime(2026, 11, 2, 0, 30, tzinfo=local)  # Still Nov 1 UTC.
    store, _, _, lists, archives = source_fixture(tmp_path, legacy_day=date(2026, 11, 2))
    with pytest.raises(ValueError, match="archive receipt is later than"):
        save(store, lists, archives, selected_at=selected)
    assert store.put_keys == []
    assert_metadata_only(store)


def test_new_selection_clock_does_not_rewrite_shared_heads_or_old_saved_clock(tmp_path):
    store, _, _, lists, archives = source_fixture(tmp_path)
    first = save(store, lists, archives)
    clear_trace(store)
    later_at = SELECTED + timedelta(hours=1)
    later = save(store, lists, archives, selected_at=later_at)
    assert later != first
    assert all(key.startswith(PREFIX + "selections/") for key in store.put_keys)
    assert_same_pair(read_edinet_input(store, first), first, lists, archives)
    assert_same_pair(read_edinet_input(store, later), later, lists, archives, later_at)
    assert_metadata_only(store)


class LostAcknowledgementStore(MemoryR2Store):
    enabled = False
    uploaded = False

    def put_file(self, key, path, **kwargs):
        if self.enabled and key.startswith(PREFIX):
            if self.uploaded:
                super().put_file(key, path, **kwargs)
            raise R2Error("lost acknowledgement")
        return super().put_file(key, path, **kwargs)


@pytest.mark.parametrize("uploaded", [False, True])
def test_uncertain_metadata_write_is_accepted_only_after_exact_readback(tmp_path, uploaded):
    store = LostAcknowledgementStore()
    store, _, _, lists, archives = source_fixture(tmp_path, store=store)
    originals = dict(store.values)
    store.enabled, store.uploaded = True, uploaded
    if uploaded:
        saved = save(store, lists, archives)
        assert_same_pair(read_edinet_input(store, saved), saved, lists, archives)
    else:
        with pytest.raises(R2Error):
            save(store, lists, archives)
    assert all(store.values[key] == value for key, value in originals.items())
    assert_metadata_only(store)


def test_saved_selection_locator_matches_its_immutable_bytes(tmp_path):
    store, _, _, lists, archives = source_fixture(tmp_path)
    saved = save(store, lists, archives)
    assert saved.descriptor is not None
    digest = saved.descriptor["selection_sha256"]
    body = store.get_bytes(PREFIX + "selections/" + digest + ".json")
    assert hashlib.sha256(body).hexdigest() == digest
    assert isinstance(json.loads(body), dict)
    assert_same_pair(read_edinet_input(store, saved), saved, lists, archives)


class ConflictingAcknowledgementStore(MemoryR2Store):
    enabled = False

    def put_file(self, key, path, **kwargs):
        if self.enabled and key.startswith(PREFIX):
            self.store_object(key, b"{}")
            raise R2Error("lost acknowledgement with a different retained object")
        return super().put_file(key, path, **kwargs)


def test_uncertain_write_with_other_bytes_is_not_accepted_or_overwritten(tmp_path):
    store = ConflictingAcknowledgementStore()
    store, _, _, lists, archives = source_fixture(tmp_path, store=store)
    originals = dict(store.values)
    store.enabled = True
    with pytest.raises(LakePublishError, match=r"changed|integrity|conflict"):
        save(store, lists, archives)
    assert all(store.values[key] == value for key, value in originals.items())
    retained = [value for key, value in store.values.items() if key.startswith(PREFIX)]
    assert len(retained) == 1
    assert retained[0].body == b"{}"
    assert_metadata_only(store)
