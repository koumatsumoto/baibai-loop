"""Atomic response replaces only the first HEAD, not the final stability check."""

import hashlib
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from tests.engine.storage.test_r2_verified_read import Body, make_store, response

from baibai_engine.storage.objects.json import MAX_PRIVATE_OBJECT_BYTES, read_private
from baibai_engine.storage.objects.r2 import LakePublishError, R2OperationStopped

PAYLOAD = b'{"mutable":"observed"}'
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def setup(monkeypatch, *, before=None, after=None, body=None, **env):
    store = make_store(**env)
    stream = Body(PAYLOAD if body is None else body)
    initial = response(stream, ContentLength=len(PAYLOAD), Metadata={"sha256": DIGEST})
    initial.update(before or {})
    final = {k: v for k, v in initial.items() if k != "Body"}
    if isinstance(after, dict):
        final.update(after)
    calls = []

    def get_object(**_):
        calls.append("GET")
        return initial

    def head_object(**_):
        calls.append("HEAD")
        if isinstance(after, Exception):
            raise after
        return final

    monkeypatch.setattr(
        store, "_client", lambda: SimpleNamespace(get_object=get_object, head_object=head_object)
    )
    return store, stream, calls


def test_same_get_bytes_and_metadata_then_head(monkeypatch):
    store, stream, calls = setup(monkeypatch)
    saved = read_private(store, "mutable")
    assert saved.payload == PAYLOAD
    assert saved.etag == "same-response"
    assert calls == ["GET", "HEAD"]
    assert stream.reads == [len(PAYLOAD) + 1]
    assert stream.closed


@pytest.mark.parametrize(
    "after",
    [
        {"ETag": '"new-version"'},
        {"Metadata": {"sha256": "b" * 64}},
        {"ContentLength": len(PAYLOAD) + 1},
        ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject"),
    ],
)
def test_overwrite_delete_or_metadata_aba_after_get_is_rejected(monkeypatch, after):
    store, stream, calls = setup(monkeypatch, after=after)
    with pytest.raises(LakePublishError):
        read_private(store, "mutable")
    assert calls == ["GET", "HEAD"]
    assert stream.closed


def test_identical_aba_has_same_contract_as_before(monkeypatch):
    # Neither reader claims a transaction lock: A -> B -> exact A between observations
    # cannot be distinguished, but the returned body remains the observed version A.
    store, _, calls = setup(monkeypatch)
    assert read_private(store, "mutable").payload == PAYLOAD
    assert calls == ["GET", "HEAD"]


@pytest.mark.parametrize(
    "before",
    [
        {"ContentLength": MAX_PRIVATE_OBJECT_BYTES + 1},
        {"ContentLength": 0},
        {"Metadata": {}},
        {"Metadata": {"sha256": "bad"}},
        {"ETag": '""'},
    ],
)
def test_invalid_headers_stop_before_body_and_followup_head(monkeypatch, before):
    store, stream, calls = setup(monkeypatch, before=before)
    with pytest.raises(LakePublishError):
        read_private(store, "mutable")
    assert calls == ["GET"]
    assert stream.reads == []
    assert stream.closed


@pytest.mark.parametrize("body", [PAYLOAD[:-1], b"!" * len(PAYLOAD), PAYLOAD + b"!"])
def test_size_and_body_digest_remain_required(monkeypatch, body):
    store, stream, calls = setup(monkeypatch, body=body)
    with pytest.raises(LakePublishError):
        read_private(store, "mutable")
    assert calls == ["GET"]
    assert stream.reads == [len(PAYLOAD) + 1]
    assert stream.closed


def test_missing_get_needs_no_followup_head(monkeypatch):
    store = make_store()
    calls = []

    def missing(**_):
        calls.append("GET")
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    monkeypatch.setattr(store, "_client", lambda: SimpleNamespace(get_object=missing))
    assert read_private(store, "mutable") is None
    assert calls == ["GET"]


@pytest.mark.parametrize("status", [403, 429])
def test_final_head_rejection_stops_next_read(tmp_path, monkeypatch, status):
    error = ClientError(
        {"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "HeadObject",
    )
    store, _, calls = setup(
        monkeypatch,
        after=error,
        R2_STOP_ON_REJECTION="1",
        R2_REJECTION_STOP_FILE=str(tmp_path / "stop"),
    )
    for _ in range(2):
        with pytest.raises(R2OperationStopped):
            read_private(store, "mutable")
    assert calls == ["GET", "HEAD"]
