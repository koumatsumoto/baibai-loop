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

    def get_bytes(self, key):
        self.pause()
        return super().get_bytes(key)


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
    assert len(store.head_keys) == 16
    assert list(fresh.iter_receipts()) == originals
    assert len(store.get_keys) == 8
    assert len(store.head_keys) == 24


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
    assert len(store.head_keys) == 2
    assert list(fresh.iter_receipts([])) == []
    assert len(store.get_keys) == 1
    assert len(store.head_keys) == 2


def test_body_change_during_parallel_read_is_not_hidden(tmp_path, monkeypatch):
    store = MemoryR2Store()
    owner = EdinetListL1(store, tmp_path)
    owner.freeze(require_existing=False)
    head = owner.publish([receipt(index + 1, str(index).encode()) for index in range(12)]).value()
    read = store.get_bytes

    def changed(key):
        body = read(key)
        store.store_object(key, gzip.compress(b"changed after GET", mtime=0))
        return body

    monkeypatch.setattr(store, "get_bytes", changed)
    with pytest.raises(LakePublishError, match="changed or failed integrity"):
        list(FixedEdinetLists(store, head).iter_receipts())
