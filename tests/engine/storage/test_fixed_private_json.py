"""Only content-addressed JSON uses the single bounded GET contract."""

import hashlib

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.json import MAX_PRIVATE_OBJECT_BYTES, read_fixed_private
from baibai_engine.storage.objects.r2 import LakePublishError

PAYLOAD = b'{"stage":"fixed"}'
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.mark.parametrize("optimized", [False, True])
def test_fixed_reader_preserves_body_etag_and_missing(optimized):
    store = MemoryR2Store()
    store.store_object("stage", PAYLOAD)
    expected = store.values["stage"].etag
    calls = []
    if optimized:

        def verified(key, *, sha256, max_bytes):
            calls.append((key, sha256, max_bytes))
            return store.get_bytes_with_metadata(key)

        store.get_verified_object = verified
    saved = read_fixed_private(store, "stage", sha256=DIGEST)
    assert saved.payload == PAYLOAD
    assert saved.etag == expected
    assert store.head_keys == ([] if optimized else ["stage", "stage"])
    assert store.get_keys == ["stage"]
    assert read_fixed_private(store, "absent", sha256=DIGEST) is None
    assert calls == (
        [("stage", DIGEST, MAX_PRIVATE_OBJECT_BYTES), ("absent", DIGEST, MAX_PRIVATE_OBJECT_BYTES)]
        if optimized
        else []
    )
    assert not store.put_keys


@pytest.mark.parametrize("optimized", [False, True])
def test_changed_body_is_not_accepted_even_if_metadata_is_self_consistent(optimized):
    store = MemoryR2Store()
    store.store_object("stage", b'{"stage":"replaced"}')
    if optimized:
        store.get_verified_object = lambda key, **_: store.get_bytes_with_metadata(key)
    with pytest.raises(LakePublishError, match="integrity"):
        read_fixed_private(store, "stage", sha256=DIGEST)


def test_fixed_response_survives_later_delete_without_inventing_new_etag():
    store = MemoryR2Store()
    store.store_object("stage", PAYLOAD)
    expected_etag = store.values["stage"].etag

    def verified(key, *, sha256, max_bytes):
        response = store.get_bytes_with_metadata(key)
        del store.values[key]
        return response

    store.get_verified_object = verified
    # The selected digest binds the returned snapshot, not a mutable current pointer.
    saved = read_fixed_private(store, "stage", sha256=DIGEST)
    assert saved.payload == PAYLOAD
    assert saved.etag == expected_etag
    assert not store.head_keys
    assert "stage" not in store.values
