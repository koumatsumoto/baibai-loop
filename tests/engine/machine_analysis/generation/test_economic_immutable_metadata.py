"""Known content hashes replace post-read HEADs, not actual payload checks."""

import hashlib

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.generation import economic_inputs as inputs
from baibai_engine.storage.objects.json import MAX_PRIVATE_OBJECT_BYTES
from baibai_engine.storage.objects.r2 import LakePublishError, R2Error, RemoteObject


def test_immutable_warm_retain_and_read_each_need_one_head_and_one_get():
    store = MemoryR2Store()
    digest = inputs._retain(store, {"fixture": "exact"})
    body = store.values[inputs._key(digest)].body
    store.head_keys.clear()
    store.get_keys.clear()
    store.put_keys.clear()
    assert inputs._retain(store, {"fixture": "exact"}) == digest
    assert inputs._read(store, digest) == body
    assert len(store.head_keys) == len(store.get_keys) == 2
    assert not store.put_keys


@pytest.mark.parametrize("damage", ["metadata", "etag", "empty", "large"])
def test_invalid_remote_metadata_stops_before_get(damage):
    class BadHead(MemoryR2Store):
        bad = False

        def head(self, key):
            value = super().head(key)
            if not self.bad or value is None:
                return value
            return RemoteObject(
                etag="" if damage == "etag" else value.etag,
                size=0
                if damage == "empty"
                else MAX_PRIVATE_OBJECT_BYTES + 1
                if damage == "large"
                else value.size,
                metadata={"sha256": "f" * 64} if damage == "metadata" else value.metadata,
                content_type=value.content_type,
            )

    store = BadHead()
    digest = inputs._retain(store, {"fixture": "exact"})
    store.bad = True
    store.get_keys.clear()
    store.put_keys.clear()
    with pytest.raises(LakePublishError, match="missing or changed"):
        inputs._read(store, digest)
    with pytest.raises(LakePublishError, match="missing or changed"):
        inputs._retain(store, {"fixture": "exact"})
    assert not store.get_keys
    assert not store.put_keys


@pytest.mark.parametrize("damage", ["different", "short", "failure"])
def test_good_metadata_cannot_substitute_for_received_bytes(damage):
    class BadGet(MemoryR2Store):
        bad = False

        def get_bytes(self, key):
            body = super().get_bytes(key)
            if not self.bad:
                return body
            if damage == "failure":
                raise R2Error("read timeout")
            return b"!" * len(body) if damage == "different" else body[:-1]

    store = BadGet()
    digest = inputs._retain(store, {"fixture": "exact"})
    store.bad = True
    store.put_keys.clear()
    with pytest.raises(LakePublishError, match=r"missing or changed|read timeout"):
        inputs._read(store, digest)
    with pytest.raises(LakePublishError, match=r"missing or changed|read timeout"):
        inputs._retain(store, {"fixture": "exact"})
    assert not store.put_keys


def test_lost_put_ack_resolves_the_same_exact_bytes_without_a_second_readback():
    class LostAck(MemoryR2Store):
        def put_file(self, *args, **kwargs):
            super().put_file(*args, **kwargs)
            raise R2Error("PUT acknowledgement lost")

    store = LostAck()
    digest = inputs._retain(store, {"fixture": "exact"})
    assert len(store.put_keys) == len(store.get_keys) == 1
    assert len(store.head_keys) == 3  # Absent, PUT receipt, immutable readback.
    assert hashlib.sha256(inputs._read(store, digest)).hexdigest() == digest
    assert inputs._retain(store, {"fixture": "exact"}) == digest
    assert len(store.put_keys) == 1


def test_expected_content_is_valid_even_if_an_etag_changes_after_receipt():
    class NewEtag(MemoryR2Store):
        def get_bytes(self, key):
            body = super().get_bytes(key)
            self.values[key].etag = "another-etag-for-the-same-bytes"
            return body

    store = NewEtag()
    digest = inputs._retain(store, {"fixture": "exact"})
    assert hashlib.sha256(inputs._read(store, digest)).hexdigest() == digest
