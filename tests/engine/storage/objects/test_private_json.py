import base64
import hashlib
from dataclasses import replace
from unittest.mock import Mock, call

import pytest

from baibai_engine.storage.objects.json import (
    MAX_PRIVATE_OBJECT_BYTES,
    SavedObject,
    put_private,
    read_private,
)
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    ObjectStore,
    RemoteObject,
)

KEY = "coordination/example/state.json"
PAYLOAD = b'{ "count": 1 }\n'
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
REMOTE = RemoteObject("v1", len(PAYLOAD), {"sha256": DIGEST}, "application/json")


def test_read_returns_exact_bytes_only_from_a_stable_generation():
    store = Mock(spec=ObjectStore)
    store.head.return_value = REMOTE
    store.get_bytes.return_value = PAYLOAD
    assert read_private(store, KEY) == SavedObject(PAYLOAD, REMOTE.etag)
    assert store.mock_calls == [call.head(KEY), call.get_bytes(KEY), call.head(KEY)]


def test_read_missing_object_does_not_request_a_body():
    store = Mock(spec=ObjectStore)
    store.head.return_value = None
    assert read_private(store, KEY) is None
    assert store.mock_calls == [call.head(KEY)]


@pytest.mark.parametrize(
    "fault", ["changed", "deleted", "truncated", "wrong_sha", "empty_etag", "empty", "oversize"]
)
def test_read_refuses_racing_or_invalid_objects(fault):
    before = REMOTE
    after = replace(REMOTE, etag="v2") if fault == "changed" else REMOTE
    if fault == "deleted":
        after = None
    if fault == "wrong_sha":
        before = replace(before, metadata={"sha256": "wrong"})
    elif fault == "empty_etag":
        before = replace(before, etag="")
    elif fault == "empty":
        before = replace(before, size=0)
    elif fault == "oversize":
        before = replace(before, size=MAX_PRIVATE_OBJECT_BYTES + 1)
    store = Mock(spec=ObjectStore)
    store.head.side_effect = [before, after]
    store.get_bytes.return_value = PAYLOAD[:-1] if fault == "truncated" else PAYLOAD
    with pytest.raises(LakePublishError, match="private JSON object"):
        read_private(store, KEY)
    store.put_file.assert_not_called()


@pytest.mark.parametrize("previous_etag", [None, "previous"])
def test_put_keeps_exact_bytes_private_mode_and_conditional_generation(previous_etag):
    store = Mock(spec=ObjectStore)
    paths = []

    def upload(key, path, **kwargs):
        paths.append(path)
        assert key == KEY
        assert path.read_bytes() == PAYLOAD
        assert path.stat().st_mode & 0o777 == 0o600
        assert kwargs == {
            "sha256": DIGEST,
            "content_md5": base64.b64encode(
                hashlib.md5(PAYLOAD, usedforsecurity=False).digest()
            ).decode(),
            "content_type": "application/json",
            "if_match": previous_etag,
            "if_none_match": previous_etag is None,
        }
        return REMOTE

    store.put_file.side_effect = upload
    assert put_private(store, KEY, PAYLOAD, previous_etag=previous_etag) == SavedObject(
        PAYLOAD, REMOTE.etag
    )
    store.put_file.assert_called_once()
    assert not paths[0].exists()


@pytest.mark.parametrize("payload", [b"", b"x" * (MAX_PRIVATE_OBJECT_BYTES + 1)])
def test_put_rejects_invalid_size_before_io(payload):
    store = Mock(spec=ObjectStore)
    with pytest.raises(ValueError, match="payload size"):
        put_private(store, KEY, payload, previous_etag=None)
    assert store.mock_calls == []


@pytest.mark.parametrize(
    "receipt",
    [replace(REMOTE, etag=""), replace(REMOTE, size=0), replace(REMOTE, metadata={})],
)
def test_put_refuses_an_invalid_write_receipt_without_retry(receipt):
    store = Mock(spec=ObjectStore)
    store.put_file.return_value = receipt
    with pytest.raises(LakePublishError, match="write receipt mismatch"):
        put_private(store, KEY, PAYLOAD, previous_etag=None)
    store.put_file.assert_called_once()


def test_put_propagates_a_conditional_conflict_without_retry():
    store = Mock(spec=ObjectStore)
    store.put_file.side_effect = LakeCASConflict("stale conditional write")
    with pytest.raises(LakeCASConflict, match="stale conditional"):
        put_private(store, KEY, PAYLOAD, previous_etag="older")
    store.put_file.assert_called_once()
