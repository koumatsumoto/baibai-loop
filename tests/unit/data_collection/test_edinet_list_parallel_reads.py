"""有限並列の原本読戻しは順序・共有body・fresh整合性検査を維持する。"""

import gzip
import time
from dataclasses import replace
from datetime import timedelta
from threading import Lock

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.unit.data_collection.test_edinet_list_native import DAY, receipt

from baibai_engine.data_collection.providers.edinet.list_native import (
    EdinetListL1,
    FixedEdinetLists,
)
from baibai_engine.storage.objects.r2 import LakePublishError


class DelayedStore(MemoryR2Store):
    def __init__(self):
        super().__init__()
        self.delay = False
        self.active = self.peak = 0
        self.lock = Lock()

    def pause(self):
        if self.delay:
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.002)
            with self.lock:
                self.active -= 1

    def head(self, key):
        self.pause()
        return super().head(key)

    def get_bytes_with_metadata(self, key):
        self.pause()
        return super().get_bytes_with_metadata(key)


def test_distinct_bodies_are_parallel_and_each_shared_body_read_once(tmp_path):
    store = DelayedStore()
    owner = EdinetListL1(store, tmp_path)
    owner.freeze(require_existing=False)
    originals = [receipt(index + 1, str(index % 8).encode()) for index in range(64)]
    head = owner.publish(originals).value()
    fresh = FixedEdinetLists(store, head)
    store.get_keys.clear()
    store.head_keys.clear()
    store.delay = True
    assert list(fresh.iter_receipts()) == originals
    assert 1 < store.peak <= 8
    assert len(store.get_keys) == 8
    assert len(store.head_keys) == 8
    assert list(fresh.iter_receipts()) == originals
    assert len(store.get_keys) == 8
    assert len(store.head_keys) == 16


def test_parallel_day_filter_does_not_read_other_days(tmp_path):
    store = MemoryR2Store()
    owner = EdinetListL1(store, tmp_path)
    owner.freeze(require_existing=False)
    originals = [
        replace(receipt(index + 1, str(index).encode()), doc_date=DAY + timedelta(days=index))
        for index in range(12)
    ]
    head = owner.publish(originals).value()
    fresh = FixedEdinetLists(store, head)
    store.get_keys.clear()
    store.head_keys.clear()
    assert list(fresh.iter_receipts([DAY + timedelta(days=3)])) == [originals[3]]
    assert len(store.get_keys) == 1
    assert len(store.head_keys) == 1
    assert list(fresh.iter_receipts([])) == []
    assert len(store.get_keys) == 1
    assert len(store.head_keys) == 1


def test_body_change_during_parallel_read_is_not_hidden(tmp_path, monkeypatch):
    store = MemoryR2Store()
    owner = EdinetListL1(store, tmp_path)
    owner.freeze(require_existing=False)
    head = owner.publish([receipt(index + 1, str(index).encode()) for index in range(12)]).value()
    read = store.get_bytes_with_metadata

    def changed(key):
        body = read(key)
        store.store_object(key, gzip.compress(b"changed after GET", mtime=0))
        return body

    monkeypatch.setattr(store, "get_bytes_with_metadata", changed)
    with pytest.raises(LakePublishError, match="changed or failed integrity"):
        list(FixedEdinetLists(store, head).iter_receipts())


def test_byte_only_store_retains_old_read_path_and_same_fixed_output(tmp_path):
    from types import SimpleNamespace

    store = MemoryR2Store()
    owner = EdinetListL1(store, tmp_path)
    owner.freeze(False)
    originals = [receipt(index + 1, str(index % 2).encode()) for index in range(6)]
    head = owner.publish(originals).value()
    legacy = SimpleNamespace(head=store.head, get_bytes=store.get_bytes)
    a, b = FixedEdinetLists(legacy, head), FixedEdinetLists(store, head)
    store.get_keys.clear()
    store.head_keys.clear()
    assert list(a.iter_receipts()) == originals
    assert (len(store.get_keys), len(store.head_keys)) == (2, 4)
    store.get_keys.clear()
    store.head_keys.clear()
    assert list(b.iter_receipts()) == originals
    assert a.identity == b.identity
    assert (len(store.get_keys), len(store.head_keys)) == (2, 2)


@pytest.mark.parametrize(
    "fault", ["missing", "fixed_digest", "metadata_digest", "size", "deleted", "metadata", "etag"]
)
def test_cold_metadata_read_rejects_invalid_or_changed_body(tmp_path, monkeypatch, fault):
    store = MemoryR2Store()
    owner = EdinetListL1(store, tmp_path)
    owner.freeze(False)
    original = receipt()
    head = owner.publish([original]).value()
    fresh = FixedEdinetLists(store, head)
    key = next(key for key in store.values if key.endswith(".body.gz"))
    saved = store.values[key]
    if fault == "missing":
        del store.values[key]
    elif fault == "fixed_digest":
        store.store_object(key, gzip.compress(b"different bytes", mtime=0))
    elif fault == "metadata_digest":
        store.values[key] = replace(saved, metadata={**saved.metadata, "sha256": "0" * 64})
    read = store.get_bytes_with_metadata

    def changed(key):
        result = read(key)
        if fault == "size":
            return result[0], replace(result[1], size=result[1].size + 1)
        if fault == "deleted":
            del store.values[key]
        elif fault == "metadata":
            store.values[key].metadata["changed"] = "after GET"
        elif fault == "etag":
            store.values[key].etag = "different-generation"
        return result

    with monkeypatch.context() as patch:
        patch.setattr(store, "get_bytes_with_metadata", changed)
        with pytest.raises(LakePublishError):
            list(fresh.iter_receipts())
    assert fresh._body_cache == {}
    store.store_object(key, original.body_gzip)
    assert list(fresh.iter_receipts()) == [original]


def test_mutable_head_and_write_readback_do_not_use_optional_get(tmp_path, monkeypatch):
    store = MemoryR2Store()

    def unexpected(key):
        pytest.fail("writer and mutable head must retain HEAD/GET/HEAD")

    monkeypatch.setattr(store, "get_bytes_with_metadata", unexpected)
    owner = EdinetListL1(store, tmp_path)
    owner.freeze(False)
    fixed = owner.publish([receipt()])
    assert owner.freeze().identity == fixed.identity
    assert list(fixed.iter_receipts()) == [receipt()]


def test_real_adapter_fixed_read_uses_get_response_then_head(tmp_path):
    from io import BytesIO

    from botocore.stub import Stubber

    from baibai_engine.storage.objects.r2 import Boto3R2Store

    memory = MemoryR2Store()
    owner = EdinetListL1(memory, tmp_path)
    owner.freeze(False)
    original = receipt()
    head = owner.publish([original]).value()
    key = next(key for key in memory.values if key.endswith(".body.gz"))
    stored = memory.values[key]
    metadata = {
        "ETag": f'"{stored.etag}"',
        "ContentLength": len(stored.body),
        "Metadata": stored.metadata,
        "ContentType": stored.content_type,
    }
    store = Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    body = BytesIO(stored.body)
    try:
        with Stubber(store._client()) as stub:
            request = {"Bucket": store.bucket, "Key": key}
            stub.add_response("get_object", {**metadata, "Body": body}, request)
            stub.add_response("head_object", metadata, request)
            stub.add_response("head_object", metadata, request)
            fixed = FixedEdinetLists(store, head)
            assert list(fixed.iter_receipts()) == [original]
            assert body.closed
            assert list(fixed.iter_receipts()) == [original]
            stub.assert_no_pending_responses()
    finally:
        store.close()
