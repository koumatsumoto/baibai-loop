"""Real read adapter with a local SDK substitute, never remote R2."""

from __future__ import annotations

import hashlib
import io
from types import SimpleNamespace

import pytest

from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError, R2Error
from tools.migrations import publish_market_schema_52 as publish


@pytest.fixture
def shell_store(monkeypatch):
    body = b'{"fixture":"fixed shell upload"}'
    state = {"body": body, "size": len(body), "etag": "e" * 32, "metadata": {}}
    calls = []
    streams = []

    def head_object(**kwargs):
        calls.append(("HEAD", kwargs["Key"]))
        return {
            "ContentLength": state["size"],
            "ETag": state["etag"],
            "Metadata": state["metadata"],
        }

    def get_object(**kwargs):
        calls.append(("GET", kwargs["Key"]))
        stream = io.BytesIO(state["body"])
        streams.append(stream)
        result = {
            "ContentLength": len(state["body"]),
            "ETag": state["etag"],
            "Metadata": state["metadata"],
            "Body": stream,
        }
        if state.get("change_after_get"):
            state["etag"] = "f" * 32
        return result

    store = Boto3R2Store(
        bucket="synthetic",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "synthetic",
            "R2_SECRET_ACCESS_KEY": "synthetic",
            "R2_STOP_ON_REJECTION": "1",
        },
    )
    client = SimpleNamespace(head_object=head_object, get_object=get_object)
    monkeypatch.setattr(store, "_client", lambda payload_bytes=None: client)
    return store, state, calls, streams


@pytest.mark.parametrize("key", [publish.MARKET, publish.MANIFEST])
def test_shell_owned_body_needs_plan_digest_not_private_sha_metadata(shell_store, key):
    store, state, calls, streams = shell_store
    expected = hashlib.sha256(state["body"]).hexdigest()
    assert publish._read(store, key, sha256=expected, max_bytes=64, etag="e" * 32) == (
        state["body"],
        "e" * 32,
    )
    assert calls == [("HEAD", key), ("GET", key), ("HEAD", key)]
    assert len(streams) == 1
    assert streams[0].closed


@pytest.mark.parametrize("key", [publish.MARKET, publish.MANIFEST])
@pytest.mark.parametrize("failure", ["sha", "etag", "changed", "oversized", "long", "short"])
def test_shell_read_still_refuses_changed_or_unbounded_input(shell_store, key, failure):
    store, state, calls, streams = shell_store
    expected = hashlib.sha256(state["body"]).hexdigest()
    if failure == "sha":
        expected = "0" * 64
    elif failure == "etag":
        state["etag"] = "f" * 32
    elif failure == "changed":
        state["change_after_get"] = True
    elif failure == "oversized":
        state["size"] = 65
    elif failure == "long":
        state["body"] += b"x"
    elif failure == "short":
        state["body"] = state["body"][:-1]
    with pytest.raises((ValueError, R2Error)):
        publish._read(store, key, sha256=expected, max_bytes=64, etag="e" * 32)
    assert calls.count(("GET", key)) == (0 if failure in {"etag", "oversized"} else 1)
    assert all(stream.closed for stream in streams)


@pytest.mark.parametrize("key", [publish.POINTER, "maintenance/market-schema-52/source/fixed"])
def test_private_pointer_and_immutable_backup_keep_sha_metadata_requirement(shell_store, key):
    store, state, calls, streams = shell_store
    expected = hashlib.sha256(state["body"]).hexdigest()
    with pytest.raises(LakePublishError, match="fixed object is missing or changed"):
        publish._read(store, key, sha256=expected, max_bytes=64, etag="e" * 32)
    assert calls == [("HEAD", key), ("GET", key)]
    assert streams[0].closed
    state["metadata"] = {"sha256": expected}
    assert (
        publish._read(store, key, sha256=expected, max_bytes=64, etag="e" * 32)[0] == state["body"]
    )
