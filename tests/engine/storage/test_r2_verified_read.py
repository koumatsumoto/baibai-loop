"""One fixed-object GET checks headers before bounded body consumption; no live HTTP."""

import hashlib
from io import BytesIO
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from baibai_engine.storage.objects import r2

PAYLOAD = b'{"fixed":"input"}'
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


class Body(BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.reads = []

    def read(self, size=-1):
        self.reads.append(size)
        assert size >= 0, "fixed-object read must be bounded"
        return super().read(size)


def make_store(**env):
    return r2.Boto3R2Store(
        bucket="fixture",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "fake",
            "R2_SECRET_ACCESS_KEY": "fake",
            **env,
        },
    )


def response(body, **overrides):
    return {
        "Body": body,
        "ETag": '"same-response"',
        "ContentLength": len(PAYLOAD),
        "Metadata": {"sha256": DIGEST},
        **overrides,
    }


def bind(monkeypatch, store, result):
    calls = []

    def get_object(**kwargs):
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(store, "_client", lambda *_: SimpleNamespace(get_object=get_object))
    return calls


def read(store):
    return store.get_verified_bytes("fixed-key", sha256=DIGEST, max_bytes=100)


def test_one_get_reads_the_fixed_body_and_closes_without_head(monkeypatch):
    store, body = make_store(), Body(PAYLOAD)
    calls = bind(monkeypatch, store, response(body))
    assert read(store) == PAYLOAD
    assert calls == [{"Bucket": "fixture", "Key": "fixed-key"}]
    assert body.reads == [len(PAYLOAD) + 1]
    assert body.closed


@pytest.mark.parametrize(
    "invalid",
    [
        {"ETag": '""'},
        {"ContentLength": 0},
        {"ContentLength": -1},
        {"ContentLength": 101},
        {"Metadata": {}},
        {"Metadata": {"sha256": "b" * 64}},
    ],
)
def test_invalid_headers_stop_before_any_body_read(monkeypatch, invalid):
    store, body = make_store(), Body(PAYLOAD)
    bind(monkeypatch, store, response(body, **invalid))
    with pytest.raises(r2.LakePublishError, match="missing or changed"):
        read(store)
    assert body.reads == []
    assert body.closed


@pytest.mark.parametrize("payload", [PAYLOAD[:-1], b"!" * len(PAYLOAD), PAYLOAD + b"!" * 200])
def test_good_headers_cannot_replace_size_and_body_digest(monkeypatch, payload):
    store, body = make_store(), Body(payload)
    bind(monkeypatch, store, response(body))
    with pytest.raises(r2.LakePublishError, match="missing or changed"):
        read(store)
    assert body.reads == [len(PAYLOAD) + 1]
    assert body.closed


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "NotFound", "403", "429"])
def test_only_missing_is_absent_and_no_application_retry(monkeypatch, code):
    store = make_store()
    error = ClientError({"Error": {"Code": code}}, "GetObject")
    calls = bind(monkeypatch, store, error)
    if code in {"NoSuchKey", "404", "NotFound"}:
        assert read(store) is None
    else:
        with pytest.raises(r2.R2Error, match="get-object failed"):
            read(store)
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["read_timeout", "missing_header"])
def test_stream_is_closed_on_read_or_header_failure(monkeypatch, fault):
    class FailedBody(Body):
        def read(self, size=-1):
            raise ReadTimeoutError(endpoint_url="https://redacted.invalid")

    store = make_store()
    body = FailedBody(PAYLOAD) if fault == "read_timeout" else Body(PAYLOAD)
    result = response(body)
    if fault == "missing_header":
        result.pop("ContentLength")
    bind(monkeypatch, store, result)
    with pytest.raises(r2.R2Error if fault == "read_timeout" else KeyError):
        read(store)
    assert body.closed


@pytest.mark.parametrize(
    ("digest", "limit"), [(None, 100), ("bad", 100), (DIGEST, 0), (DIGEST, -1), (DIGEST, True)]
)
def test_invalid_expected_contract_never_calls_client(monkeypatch, digest, limit):
    store = make_store()
    monkeypatch.setattr(store, "_client", lambda *_: pytest.fail("client must not start"))
    with pytest.raises(ValueError, match="invalid fixed object read contract"):
        store.get_verified_bytes("fixed-key", sha256=digest, max_bytes=limit)


@pytest.mark.parametrize("status", [403, 429])
def test_strict_rejection_persists_and_stops_next_verified_get(tmp_path, monkeypatch, status):
    stop = tmp_path / "rejection.stop"
    store = make_store(R2_STOP_ON_REJECTION="1", R2_REJECTION_STOP_FILE=str(stop))
    error = ClientError(
        {"Error": {"Code": str(status)}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "GetObject",
    )
    calls = bind(monkeypatch, store, error)
    for _ in range(2):
        with pytest.raises(r2.R2OperationStopped):
            read(store)
    assert len(calls) == 1
    assert stop.read_text() == f"{status}\n"


def test_serial_scope_keeps_completion_spacing_and_stops_after_429(monkeypatch):
    store = make_store()
    clock, calls, bodies = [10.0], [], []
    monkeypatch.setattr(r2.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(r2.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))

    def get_object(**kwargs):
        calls.append(clock[0])
        if len(calls) == 3:
            raise ClientError(
                {"Error": {"Code": "429"}, "ResponseMetadata": {"HTTPStatusCode": 429}},
                "GetObject",
            )
        body = Body(PAYLOAD)
        bodies.append(body)
        return response(body)

    monkeypatch.setattr(store, "_client", lambda *_: SimpleNamespace(get_object=get_object))

    def run():
        with store.serial_requests(min_interval_seconds=0.25):
            assert read(store) == read(store) == PAYLOAD
            with pytest.raises(r2.R2Error, match="get-object failed"):
                read(store)
            with pytest.raises(r2.R2Error, match="serial request scope stopped"):
                read(store)

    with pytest.raises(r2.R2Error, match="serial request scope stopped"):
        run()
    assert calls == [10.0, 10.25, 10.5]
    assert all(body.closed for body in bodies)


def test_verified_object_keeps_metadata_from_the_same_response(monkeypatch):
    store, body = make_store(), Body(PAYLOAD)
    calls = bind(monkeypatch, store, response(body))
    payload, metadata = store.get_verified_object("fixed-key", sha256=DIGEST, max_bytes=100)
    assert payload == PAYLOAD
    assert metadata.etag == "same-response"
    assert metadata.size == len(PAYLOAD)
    assert metadata.metadata == {"sha256": DIGEST}
    assert len(calls) == 1
    assert body.closed
