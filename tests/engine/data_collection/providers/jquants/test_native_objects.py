"""Native byte readback remains authoritative with the adapter HEAD disabled."""

import base64
import hashlib
import io

import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from baibai_engine.data_collection.providers.jquants.native_objects import _put
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakeCASConflict, LakePublishError

KEY = "l1/native-test/head.json"
PAYLOAD = b'{"receipt":"original"}'


@pytest.fixture(params=[False, True], ids=["native-readback", "shared-default"])
def wire(request, monkeypatch):
    store = Boto3R2Store(
        bucket="local-fixture",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "fixture",
            "R2_SECRET_ACCESS_KEY": "fixture",
        },
        **({"verify_put_with_head": False} if not request.param else {}),
    )
    assert store.verify_put_with_head is request.param
    client = store._client()
    monkeypatch.setattr(store, "_client", lambda *_: client)
    try:
        with Stubber(client) as stub:
            yield store, stub
            stub.assert_no_pending_responses()
    finally:
        store.close()


def expect_put(stub, *, previous=None, failure=None):
    md5 = base64.b64encode(hashlib.md5(PAYLOAD, usedforsecurity=False).digest()).decode()
    params = {
        "Bucket": "local-fixture",
        "Key": KEY,
        "Body": ANY,
        "ContentType": "application/octet-stream",
        "ContentMD5": md5,
        "Metadata": {
            "sha256": hashlib.sha256(PAYLOAD).hexdigest(),
            "content-md5": md5,
            "integrity": "content-md5-v1",
        },
        **({"IfNoneMatch": "*"} if previous is None else {"IfMatch": previous}),
    }
    if failure in {"RequestTimeout", "PreconditionFailed"}:
        stub.add_client_error(
            "put_object",
            service_error_code=failure,
            http_status_code=504 if failure == "RequestTimeout" else 412,
            expected_params=params,
        )
    else:
        stub.add_response(
            "put_object", {} if failure == "missing-etag" else {"ETag": '"new"'}, params
        )


def expect_head(stub, payload=PAYLOAD, *, etag="new", size=None):
    stub.add_response(
        "head_object",
        {
            "ETag": f'"{etag}"',
            "ContentLength": len(payload) if size is None else size,
            "Metadata": {"sha256": hashlib.sha256(payload).hexdigest()},
        },
        {"Bucket": "local-fixture", "Key": KEY},
    )


def expect_read(stub, payload=PAYLOAD, *, fault=None):
    expect_head(stub, payload, size=len(payload) + 1 if fault == "size" else None)
    body = payload.replace(b"original", b"tampered") if fault == "bytes" else payload
    stub.add_response(
        "get_object",
        {"Body": StreamingBody(io.BytesIO(body), len(body))},
        {"Bucket": "local-fixture", "Key": KEY},
    )
    expect_head(stub, payload, etag="changed-during-read" if fault == "etag" else "new")


@pytest.mark.parametrize("previous", [None, "prior"])
def test_conditional_native_put_reads_the_same_bytes_without_a_redundant_head(
    wire, tmp_path, previous
):
    store, stub = wire
    expect_put(stub, previous=previous)
    if store.verify_put_with_head:
        expect_head(stub)
    expect_read(stub)

    _put(store, KEY, PAYLOAD, tmp_path, etag=previous)

    assert (tmp_path / (hashlib.sha256(PAYLOAD).hexdigest() + ".object")).read_bytes() == PAYLOAD


@pytest.mark.parametrize("fault", ["bytes", "size", "etag"])
def test_native_readback_rejects_changed_bytes_size_or_etag(wire, tmp_path, fault):
    store, stub = wire
    expect_put(stub)
    if store.verify_put_with_head:
        expect_head(stub)
    expect_read(stub, fault=fault)

    with pytest.raises(LakePublishError, match="changed or failed integrity"):
        _put(store, KEY, PAYLOAD, tmp_path, etag=None)


@pytest.mark.parametrize("failure", ["RequestTimeout", "missing-etag"])
def test_lost_native_put_acknowledgement_is_resolved_without_republishing(wire, tmp_path, failure):
    store, stub = wire
    expect_put(stub, failure=failure)
    if store.verify_put_with_head and failure == "missing-etag":
        expect_head(stub)
    expect_read(stub)

    _put(store, KEY, PAYLOAD, tmp_path, etag=None)


@pytest.mark.parametrize("already_saved", [True, False])
def test_native_cas_conflict_accepts_only_the_exact_same_payload(wire, tmp_path, already_saved):
    store, stub = wire
    expect_put(stub, previous="prior", failure="PreconditionFailed")
    expect_read(stub, PAYLOAD if already_saved else b'{"receipt":"another-writer"}')

    if already_saved:
        _put(store, KEY, PAYLOAD, tmp_path, etag="prior")
    else:
        with pytest.raises(LakeCASConflict, match="native L1 writer changed"):
            _put(store, KEY, PAYLOAD, tmp_path, etag="prior")


def test_unwritten_native_put_retries_the_identical_conditional_bytes(wire, tmp_path):
    store, stub = wire
    expect_put(stub, failure="RequestTimeout")
    stub.add_client_error(
        "head_object",
        service_error_code="404",
        http_status_code=404,
        expected_params={"Bucket": "local-fixture", "Key": KEY},
    )
    expect_put(stub)
    if store.verify_put_with_head:
        expect_head(stub)
    expect_read(stub)

    _put(store, KEY, PAYLOAD, tmp_path, etag=None)
