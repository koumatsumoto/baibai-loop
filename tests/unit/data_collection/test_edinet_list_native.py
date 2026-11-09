"""EDINET一覧原受領の保全と条件付き公開。"""

import gzip
import hashlib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from uuid import UUID

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.edinet.list_native import (
    HEAD_KEY,
    EdinetListL1,
    FixedEdinetLists,
    NativeListReceipt,
)
from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.storage.json import canonical_json
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, R2Error

DAY = date(2026, 1, 1)
TIME = datetime(2026, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)


def receipt(index=1, body=b"raw malformed {", **kwargs):
    return NativeListReceipt.from_response(
        DAY,
        EdinetResponse(TIME + timedelta(microseconds=index), body),
        UUID(int=index).hex,
        **kwargs,
    )


def owner(tmp_path, store=None):
    store = store or MemoryR2Store()
    result = EdinetListL1(store, tmp_path)
    result.freeze(require_existing=False)
    return result, store


def test_explicit_empty_bootstrap_and_missing_default(tmp_path):
    writer = EdinetListL1(MemoryR2Store(), tmp_path)
    with pytest.raises(LakePublishError, match="bootstrap"):
        writer.freeze()
    with pytest.raises(LakePublishError, match="bootstrap"):
        writer.publish([])
    writer.freeze(require_existing=False)
    empty = writer.publish([])
    assert empty.value() == {"format": "edinet-list-native-v1", "receipts": []}
    assert writer.freeze().identity == empty.identity


def test_a_b_a_and_identical_bytes_distinct_receipts(tmp_path):
    writer, store = owner(tmp_path)
    receipts = [receipt(3, b"A"), receipt(1, b"A"), receipt(2, b"B")]
    fixed = writer.publish(receipts)
    assert list(fixed.iter_receipts()) == sorted(receipts, key=lambda r: r.received_at)
    assert len([key for key in store.put_keys if key != HEAD_KEY]) == 2
    previous_puts = list(store.put_keys)
    assert writer.publish(receipts).identity == fixed.identity
    assert store.put_keys == previous_puts
    assert list(fixed.iter_receipts([date(2020, 1, 1)])) == []


def test_preserves_legacy_gzip_timestamp_and_fixed_head(tmp_path):
    writer, store = owner(tmp_path)
    legacy = replace(receipt(), body_gzip=gzip.compress(b"not JSON", mtime=12345))
    fixed = writer.publish([legacy])
    value = fixed.value()
    restored = FixedEdinetLists(store, canonical_json(value))
    value["receipts"].clear()
    assert restored.identity == fixed.identity
    writer.publish([receipt(2)])
    del store.values[HEAD_KEY]
    (actual,) = fixed.iter_receipts()
    assert actual == legacy
    assert actual.body_gzip != gzip.compress(b"not JSON", mtime=0)
    assert actual.decoded().body == b"not JSON"
    assert actual.received_at == TIME + timedelta(microseconds=1)


@pytest.mark.parametrize(
    "change",
    [
        {"body_gzip": gzip.compress(b"different", mtime=0)},
        {"doc_date": date(2026, 2, 2)},
        {"received_at": TIME},
    ],
)
def test_identity_conflict_never_overwrites(tmp_path, change):
    writer, store = owner(tmp_path)
    original = receipt()
    fixed = writer.publish([original])
    puts = list(store.put_keys)
    with pytest.raises(ValueError, match="identity conflicts"):
        writer.publish([replace(original, **change)])
    assert store.put_keys == puts
    assert writer.freeze().identity == fixed.identity


def test_competing_head_writer_keeps_winner_and_local_bytes(tmp_path):
    class Competing(MemoryR2Store):
        race = False

        def put_file(self, key, path, **kwargs):
            if key == HEAD_KEY and self.race:
                self.store_object(key, b'{"winner":true}')
            return super().put_file(key, path, **kwargs)

    writer, store = owner(tmp_path, Competing())
    writer.publish([receipt()])
    store.race = True
    with pytest.raises(LakeCASConflict):
        writer.publish([receipt(2)])
    assert store.get_bytes(HEAD_KEY) == b'{"winner":true}'
    assert (tmp_path / "pending.json").exists()


@pytest.mark.parametrize("target", ["body", "head"])
@pytest.mark.parametrize("acknowledged", [False, True])
def test_failed_and_uncertain_put_reconcile_exact_bytes(tmp_path, target, acknowledged):
    class Faulty(MemoryR2Store):
        fail = True

        def put_file(self, key, path, **kwargs):
            matches = (key == HEAD_KEY) == (target == "head")
            if self.fail and matches:
                self.fail = False
                if acknowledged:
                    super().put_file(key, path, **kwargs)
                raise R2Error("lost acknowledgement")
            return super().put_file(key, path, **kwargs)

    writer, store = owner(tmp_path, Faulty())
    if acknowledged:
        fixed = writer.publish([receipt()])
    else:
        with pytest.raises(R2Error):
            writer.publish([receipt()])
        assert HEAD_KEY not in store.values
        fresh = EdinetListL1(store, tmp_path)
        fresh.freeze(require_existing=False)
        fixed = fresh.publish([receipt()])
    assert list(fixed.iter_receipts()) == [receipt()]
    body_puts = [key for key in store.put_keys if key != HEAD_KEY]
    assert len(body_puts) == 1


def test_failed_readback_never_publishes_head(tmp_path):
    class Faulty(MemoryR2Store):
        def get_bytes(self, key):
            if key != HEAD_KEY:
                raise R2Error("readback unavailable")
            return super().get_bytes(key)

    writer, store = owner(tmp_path, Faulty())
    with pytest.raises(R2Error, match="readback"):
        writer.publish([receipt()])
    assert HEAD_KEY not in store.values
    assert list(tmp_path.glob("*.object"))


@pytest.mark.parametrize(
    "head", [b"not json", b"[]", b"{}", b'{"format":"other-source","receipts":[]}']
)
def test_malformed_head_is_not_empty_bootstrap(tmp_path, head):
    store = MemoryR2Store()
    store.store_object(HEAD_KEY, head)
    with pytest.raises(ValueError, match=r"invalid|Expecting"):
        EdinetListL1(store, tmp_path).freeze(require_existing=False)


def test_receipt_requires_utc_and_valid_gzip():
    with pytest.raises(ValueError, match="UTC"):
        replace(receipt(), received_at=TIME.replace(tzinfo=None))
    with pytest.raises(ValueError, match="UTC"):
        replace(receipt(), received_at=TIME.astimezone(timezone(timedelta(hours=9))))
    with pytest.raises(gzip.BadGzipFile):
        replace(receipt(), body_gzip=b"not gzip")


def test_head_rejects_bad_metadata(tmp_path):
    writer, store = owner(tmp_path)
    head = writer.publish([receipt()]).value()
    head["receipts"][0]["sha256"] = hashlib.sha256(b"").hexdigest().upper()
    with pytest.raises(ValueError, match="hash"):
        FixedEdinetLists(store, head)


def test_resume_work_after_competing_writer_joins_exact_receipts(tmp_path):
    class Competing(MemoryR2Store):
        race = False

        def put_file(self, key, path, **kwargs):
            if key == HEAD_KEY and self.race:
                self.race = False
                EdinetListL1(self, tmp_path / "other").publish([receipt(3)])
            return super().put_file(key, path, **kwargs)

    writer, store = owner(tmp_path, Competing())
    writer.publish([receipt()])
    store.race = True
    with pytest.raises(LakeCASConflict):
        writer.publish([receipt(2)])
    with pytest.raises(LakePublishError, match="resume_work"):
        writer.publish([receipt(4)])
    fixed = EdinetListL1(store, tmp_path).resume_work()
    assert list(fixed.iter_receipts()) == [receipt(), receipt(2), receipt(3)]
    assert not (tmp_path / "pending.json").exists()


def test_resume_bootstrap_after_failed_head_put_without_new_response(tmp_path):
    class Faulty(MemoryR2Store):
        fail = True

        def put_file(self, key, path, **kwargs):
            if key == HEAD_KEY and self.fail:
                self.fail = False
                raise R2Error("failed")
            return super().put_file(key, path, **kwargs)

    writer, store = owner(tmp_path, Faulty())
    with pytest.raises(R2Error):
        writer.publish([receipt()])
    fixed = EdinetListL1(store, tmp_path).resume_work()
    assert list(fixed.iter_receipts()) == [receipt()]
    assert len([k for k in store.put_keys if k != HEAD_KEY]) == 1


def test_standalone_fixed_reader_fetches_shared_body_once(tmp_path):
    writer, store = owner(tmp_path)
    fixed = writer.publish([receipt(), receipt(2)])
    store.get_keys.clear()
    standalone = FixedEdinetLists(store, fixed.value())
    assert list(standalone.iter_receipts()) == [receipt(), receipt(2)]
    assert len(store.get_keys) == 1


def test_owner_freeze_read_and_publish_share_verified_body_cache(tmp_path):
    writer, store = owner(tmp_path)
    writer.publish([receipt(), receipt(2)])
    fresh = EdinetListL1(store, tmp_path / "fresh")
    store.get_keys.clear()
    assert list(fresh.freeze().iter_receipts()) == [receipt(), receipt(2)]
    assert len([key for key in store.get_keys if key != HEAD_KEY]) == 1
    fresh.publish([receipt(3)])
    assert len([key for key in store.get_keys if key != HEAD_KEY]) == 1
    other = EdinetListL1(store, tmp_path / "other")
    assert len(list(other.freeze().iter_receipts())) == 3
    assert len([key for key in store.get_keys if key != HEAD_KEY]) == 2


@pytest.mark.parametrize("tamper", ["missing", "body", "metadata"])
def test_verified_cache_rejects_changed_object_and_blocks_new_head(tmp_path, tamper):
    writer, store = owner(tmp_path)
    fixed = writer.publish([receipt()])
    assert list(fixed.iter_receipts()) == [receipt()]
    key = next(key for key in store.values if key != HEAD_KEY)
    if tamper == "missing":
        del store.values[key]
    elif tamper == "body":
        store.store_object(key, gzip.compress(b"changed", mtime=0))
    else:
        store.values[key].metadata["sha256"] = "0" * 64
    with pytest.raises(LakePublishError):
        list(fixed.iter_receipts())
    with pytest.raises(LakePublishError):
        writer.publish([receipt(2)])
    assert store.values[HEAD_KEY].body == canonical_json(fixed.value())
