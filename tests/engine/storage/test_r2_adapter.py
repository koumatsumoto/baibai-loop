"""R2 transfer bounds and conditional errors belong to the object adapter."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.httpsession import URLLib3Session

from baibai_engine.storage.objects import r2 as object_storage
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    LakeCASConflict,
    LakePublishError,
    R2Error,
    R2OperationStopped,
)


def test_r2_account_id_rejects_endpoint_injection() -> None:
    with pytest.raises(LakePublishError, match="32 lowercase hexadecimal"):
        Boto3R2Store(
            bucket="baibai-stores",
            env={
                "R2_ACCOUNT_ID": "safe@attacker.example/",
                "R2_ACCESS_KEY_ID": "not-logged",
                "R2_SECRET_ACCESS_KEY": "not-logged",
            },
        )


@pytest.mark.parametrize(
    ("code", "status"),
    [("ConditionalRequestConflict", 409), ("PreconditionFailed", 412)],
)
def test_r2_adapter_classifies_conflict_and_timeout(
    monkeypatch: pytest.MonkeyPatch, code: str, status: int
) -> None:
    store = Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    conflict = ClientError(
        {
            "Error": {"Code": code, "Message": "secret"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutObject",
    )
    with pytest.raises(LakeCASConflict):
        object_storage._raise_r2_error("put-object", conflict)

    class TimeoutClient:
        def head_object(self, **_: object) -> object:
            raise ReadTimeoutError(endpoint_url="https://redacted.invalid")

    monkeypatch.setattr(store, "_client", lambda *_: TimeoutClient())
    with pytest.raises(R2Error, match="timed out"):
        store.head("test")


def test_r2_adapter_sends_conditional_put_and_classifies_404(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    requests: list[dict[str, object]] = []

    class RecordingClient:
        def put_object(self, **kwargs: object) -> object:
            requests.append(kwargs)
            return {"ETag": '"put"'}

        def head_object(self, **_: object) -> object:
            if not requests:
                raise ClientError(
                    {
                        "Error": {"Code": "404", "Message": "credential-must-not-leak"},
                        "ResponseMetadata": {"HTTPStatusCode": 404},
                    },
                    "HeadObject",
                )
            return {
                "ETag": '"put"',
                "ContentLength": 3,
                "Metadata": {
                    "sha256": "a" * 64,
                    "content-md5": "md5",
                    "integrity": "content-md5-v1",
                },
                "ContentType": "application/json",
            }

        def get_object(self, **_: object) -> object:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")

    monkeypatch.setattr(store, "_client", lambda *_: RecordingClient())
    assert store.head("missing") is None
    with pytest.raises(R2Error, match="get-object failed"):
        store.get_bytes("missing")
    payload = tmp_path / "payload"
    payload.write_bytes(b"one")
    store.put_file(
        "key",
        payload,
        sha256="a" * 64,
        content_md5="md5",
        content_type="application/json",
        if_match="start-etag",
    )

    assert requests[0]["IfMatch"] == '"start-etag"'
    assert "IfNoneMatch" not in requests[0]
    assert requests[0]["ContentMD5"] == "md5"


@pytest.mark.parametrize("etag", [None, "a" * 32, "a" * 32 + "-3", "opaque-generation-2"])
def test_r2_conditional_header_survives_sdk_serialization(tmp_path, monkeypatch, etag):
    from botocore.serialize import create_serializer
    from botocore.session import get_session

    store = metadata_store()
    model = get_session().get_service_model("s3")
    serializer = create_serializer(model.metadata["protocol"])
    headers = []

    class Client:
        def put_object(self, **kwargs):
            request = serializer.serialize_to_request(kwargs, model.operation_model("PutObject"))
            headers.append(request["headers"])
            return {"ETag": '"next"'}

        def head_object(self, **kwargs):
            return {"ETag": '"next"', "ContentLength": 3}

    monkeypatch.setattr(store, "_client", lambda *_: Client())
    payload = tmp_path / "payload"
    payload.write_bytes(b"one")
    store.put_file(
        "key",
        payload,
        sha256="sha",
        content_md5="md5",
        content_type="application/json",
        if_match=etag,
        if_none_match=etag is None,
    )
    assert len(headers) == 1
    if etag is None:
        assert headers[0]["If-None-Match"] == "*"
        assert "If-Match" not in headers[0]
    else:
        assert headers[0]["If-Match"] == f'"{etag}"'
        assert "If-None-Match" not in headers[0]


@pytest.mark.parametrize(
    ("failure", "error", "finish"),
    [
        ("none", None, ["lease", "complete", "head"]),
        ("part-etag", R2Error, ["lease", "abort"]),
        ("abort-401", R2Error, ["lease", "abort"]),
        ("abort-403", R2OperationStopped, ["lease", "abort"]),
        ("abort-lease-401", R2Error, ["lease"]),
        ("sha256", R2Error, ["lease", "abort"]),
        ("whole-md5", R2Error, ["lease", "abort"]),
        ("lease-expired", ValueError, ["lease"]),
        ("part-403", R2OperationStopped, []),
        ("part-429", R2OperationStopped, []),
        ("part-cancel", KeyboardInterrupt, []),
        ("complete-conflict", LakeCASConflict, ["lease", "complete"]),
        ("complete-lost-ack", R2Error, ["lease", "complete"]),
        ("complete-invalid-etag", R2Error, ["lease", "complete"]),
        ("head-other-etag", R2Error, ["lease", "complete", "head"]),
        ("head-other-size", R2Error, ["lease", "complete", "head"]),
        ("head-other-sha", R2Error, ["lease", "complete", "head"]),
    ],
)
def test_immutable_multipart_uses_conditional_complete_and_fails_closed(
    tmp_path, monkeypatch, failure, error, finish
):
    from botocore.serialize import create_serializer
    from botocore.session import get_session

    key = "machine/payloads/00000000000000000000000000000001/runs.sqlite"
    raw = b"abcdefghi"
    content_type = "application/vnd.sqlite3"
    source = tmp_path / "runs.sqlite"
    source.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    md5 = base64.b64encode(hashlib.md5(raw, usedforsecurity=False).digest()).decode()
    chunks = [b"abcd", b"efgh", b"i"]
    digests = [hashlib.md5(chunk, usedforsecurity=False).digest() for chunk in chunks]
    composite = hashlib.md5(b"".join(digests), usedforsecurity=False).hexdigest() + "-3"
    monkeypatch.setattr(object_storage, "_MULTIPART_PART_BYTES", 4)
    store = Boto3R2Store(
        bucket="isolated-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "unused",
            "R2_SECRET_ACCESS_KEY": "unused",
            "R2_STOP_ON_REJECTION": "1",
        },
        verify_put_with_head=False,
    )
    model = get_session().get_service_model("s3")
    serializer = create_serializer(model.metadata["protocol"])
    events = []
    client_events = get_session().get_component("event_emitter")
    metadata: dict[str, str] = {}
    identity = {"Bucket": "isolated-test", "Key": key, "UploadId": "one-fresh-upload"}

    def while_held():
        events.append("lease")
        if failure == "lease-expired" and events.count("lease") > 1:
            raise ValueError("lease expired")
        if failure == "abort-lease-401" and events.count("lease") > 1:
            raise R2Error("lease check was rejected", http_status=401)

    class RecordingClient:
        meta = SimpleNamespace(events=client_events)

        def create_multipart_upload(self, **kwargs):
            events.append("create")
            request = serializer.serialize_to_request(
                kwargs, model.operation_model("CreateMultipartUpload")
            )
            assert "If-None-Match" not in request["headers"]
            assert request["headers"]["Content-Type"] == content_type
            metadata.update(kwargs["Metadata"])
            return {"UploadId": identity["UploadId"]}

        def upload_part(self, **kwargs):
            number = kwargs["PartNumber"]
            events.append(f"part-{number}")
            assert all(kwargs[name] == item for name, item in identity.items())
            assert kwargs["Body"] == chunks[number - 1]
            request = serializer.serialize_to_request(kwargs, model.operation_model("UploadPart"))
            assert request["headers"]["Content-MD5"] == (
                base64.b64encode(digests[number - 1]).decode()
            )
            assert int(request["headers"]["Content-Length"]) == len(chunks[number - 1])
            if failure in {"part-403", "part-429"}:
                status = int(failure[-3:])
                raise ClientError(
                    {
                        "Error": {"Code": str(status)},
                        "ResponseMetadata": {"HTTPStatusCode": status},
                    },
                    "UploadPart",
                )
            if failure == "part-cancel":
                raise KeyboardInterrupt
            bad_etag = failure == "part-etag" or failure.startswith("abort-")
            etag = "wrong" if bad_etag else digests[number - 1].hex()
            return {"ETag": f'"{etag}"'}

        def complete_multipart_upload(self, **kwargs):
            events.append("complete")
            assert all(kwargs[name] == item for name, item in identity.items())
            request = serializer.serialize_to_request(
                kwargs, model.operation_model("CompleteMultipartUpload")
            )
            assert request["headers"]["If-None-Match"] == "*"
            assert "If-Match" not in request["headers"]
            assert kwargs["MultipartUpload"]["Parts"] == [
                {"PartNumber": i, "ETag": f'"{digest.hex()}"'}
                for i, digest in enumerate(digests, start=1)
            ]
            if failure == "complete-conflict":
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "CompleteMultipartUpload",
                )
            if failure == "complete-lost-ack":
                raise ReadTimeoutError(endpoint_url="https://unused.invalid")
            return {
                "ETag": '"unexpected"' if failure == "complete-invalid-etag" else f'"{composite}"'
            }

        def abort_multipart_upload(self, **kwargs):
            events.append("abort")
            assert kwargs == identity
            if failure in {"abort-401", "abort-403"}:
                status = int(failure[-3:])
                raise ClientError(
                    {
                        "Error": {"Code": str(status)},
                        "ResponseMetadata": {"HTTPStatusCode": status},
                    },
                    "AbortMultipartUpload",
                )

        def head_object(self, **kwargs):
            events.append("head")
            assert kwargs == {name: identity[name] for name in ("Bucket", "Key")}
            return {
                "ETag": '"same-size-different-etag"'
                if failure == "head-other-etag"
                else f'"{composite}"',
                "ContentLength": len(raw) + (failure == "head-other-size"),
                "ContentType": content_type,
                "Metadata": metadata
                if failure != "head-other-sha"
                else {**metadata, "sha256": "0"},
            }

    monkeypatch.setattr(store, "_client", lambda *_: RecordingClient())

    def publish():
        return store.put_immutable_multipart_file(
            key,
            source,
            size=len(raw),
            sha256=sha if failure != "sha256" else "0" * 64,
            content_md5=md5 if failure != "whole-md5" else "wrong",
            content_type=content_type,
            while_held=while_held,
        )

    if error is None:
        result = publish()
        assert result.etag == composite
        assert result.size == len(raw)
        assert result.metadata == {"sha256": sha, "content-md5": md5, "integrity": "part-md5-v1"}
    else:
        with pytest.raises(error) as caught:
            publish()
        if failure in {"abort-401", "abort-lease-401"}:
            assert caught.value.http_status == 401
    partial = failure in {
        "part-etag",
        "part-403",
        "part-429",
        "part-cancel",
        "abort-401",
        "abort-403",
        "abort-lease-401",
    }
    assert events == [
        "lease",
        "create",
        "part-1",
        *([] if partial else ["part-2", "part-3"]),
        *finish,
    ]


@pytest.mark.parametrize(
    ("status", "code", "region"),
    [
        (301, "PermanentRedirect", "us-west-2"),
        (301, "PermanentRedirect", None),
        (200, "AuthorizationHeaderMalformed", "us-west-2"),
        (200, "InternalError", None),
        (403, "AuthorizationHeaderMalformed", "us-west-2"),
        (429, "AuthorizationHeaderMalformed", "us-west-2"),
    ],
)
def test_complete_never_sends_a_redirect_or_discovery_request(
    tmp_path, monkeypatch, status, code, region
):
    # This uses the pinned SDK's actual signing and response/redirect handlers.
    # Stubber would bypass the retry handlers that used to resend Complete.
    raw = b"abcdefghi"
    source = tmp_path / "runs.sqlite"
    source.write_bytes(raw)
    calls = []
    monkeypatch.setattr(object_storage, "_MULTIPART_PART_BYTES", 4)

    class Body:
        def __init__(self, value):
            self.value = value

        def stream(self, *_args, **_kwargs):
            return iter([self.value])

    def send(_session, request):
        query = parse_qs(urlparse(request.url).query, keep_blank_values=True)
        if request.method == "POST" and "uploads" in query:
            calls.append("create")
            return AWSResponse(
                request.url,
                200,
                {"content-type": "application/xml"},
                Body(
                    b"<InitiateMultipartUploadResult><UploadId>one</UploadId>"
                    b"</InitiateMultipartUploadResult>"
                ),
            )
        if request.method == "PUT" and "partNumber" in query:
            calls.append("part")
            payload = request.body if isinstance(request.body, bytes) else request.body.read()
            etag = hashlib.md5(payload, usedforsecurity=False).hexdigest()
            return AWSResponse(request.url, 200, {"etag": f'"{etag}"'}, Body(b""))
        if request.method == "POST" and query.get("uploadId") == ["one"]:
            calls.append("complete")
            assert calls.count("complete") == 1
            condition = request.headers["If-None-Match"]
            authorization = request.headers["Authorization"]
            assert condition in {b"*", "*"}
            assert "if-none-match" in (
                authorization.decode() if isinstance(authorization, bytes) else authorization
            )
            headers = {"content-type": "application/xml"}
            if status == 301 and region:
                headers["x-amz-bucket-region"] = region
            xml_region = f"<Region>{region}</Region>" if region else ""
            error = f"<Error><Code>{code}</Code>{xml_region}</Error>".encode()
            return AWSResponse(request.url, status, headers, Body(error))
        pytest.fail(f"unexpected follow-up request: {request.method}")

    monkeypatch.setattr(URLLib3Session, "send", send)
    stop_file = tmp_path / "stopped"
    store = Boto3R2Store(
        bucket="isolated-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "offline-test",
            "R2_SECRET_ACCESS_KEY": "offline-test",
            "R2_STOP_ON_REJECTION": "1",
            "R2_REJECTION_STOP_FILE": str(stop_file),
        },
    )
    expected_error = R2OperationStopped if code == "AuthorizationHeaderMalformed" else R2Error
    with pytest.raises(expected_error):
        store.put_immutable_multipart_file(
            "multipart-test",
            source,
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            content_md5=base64.b64encode(hashlib.md5(raw, usedforsecurity=False).digest()).decode(),
            content_type="application/vnd.sqlite3",
            while_held=lambda: None,
        )
    assert calls == ["create", "part", "part", "part", "complete"]
    if status in {403, 429}:
        assert stop_file.read_text() == f"{status}\n"
    else:
        assert not stop_file.exists()


@pytest.mark.parametrize("existing", [False, True])
def test_r2_download_stream_failure_never_exposes_partial_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    store = Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    target = tmp_path / "cache.json"
    if existing:
        target.write_bytes(b"known-good")

    class BrokenBody:
        reads = 0

        def read(self, _: int) -> bytes:
            self.reads += 1
            if self.reads == 1:
                return b"partial"
            raise ReadTimeoutError(endpoint_url="https://redacted.invalid")

        def close(self) -> None:
            pass

    class Client:
        def get_object(self, **_: object) -> object:
            return {"Body": BrokenBody()}

    monkeypatch.setattr(store, "_client", lambda *_: Client())
    with pytest.raises(R2Error, match="timed out"):
        store.download_file("key", target, expect_bytes=10)

    if existing:
        assert target.read_bytes() == b"known-good"
    else:
        assert not target.exists()
    assert list(tmp_path.glob(".cache.json.*.part")) == []


@pytest.mark.parametrize(("payload", "expected"), [(b"short", 6), (b"too-long", 7)])
def test_r2_download_rejects_size_mismatch_without_replacing_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    expected: int,
) -> None:
    store = Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    target = tmp_path / "cache.json"
    target.write_bytes(b"known-good")

    class Body:
        returned = False

        def read(self, _: int) -> bytes:
            if self.returned:
                return b""
            self.returned = True
            return payload

        def close(self) -> None:
            pass

    class Client:
        def get_object(self, **_: object) -> object:
            return {"Body": Body()}

    monkeypatch.setattr(store, "_client", lambda *_: Client())
    with pytest.raises(R2Error, match="size mismatch"):
        store.download_file("key", target, expect_bytes=expected)

    assert target.read_bytes() == b"known-good"


def test_r2_download_atomically_installs_complete_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    target = tmp_path / "nested" / "cache.json"

    class Body:
        chunks = iter((b"complete", b""))

        def read(self, _: int) -> bytes:
            return next(self.chunks)

        def close(self) -> None:
            pass

    class Client:
        def get_object(self, **_: object) -> object:
            return {"Body": Body()}

    monkeypatch.setattr(store, "_client", lambda *_: Client())
    store.download_file("key", target, expect_bytes=8)

    assert target.read_bytes() == b"complete"
    assert list(target.parent.glob(".cache.json.*.part")) == []


def test_r2_download_stops_at_the_snapshot_size_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )
    target = tmp_path / "cache.json"
    target.write_bytes(b"known-good")

    class Body:
        reads = 0
        closed = False

        def read(self, size: int) -> bytes:
            assert size == 8 * 1024 * 1024
            self.reads += 1
            if self.reads > 2:
                pytest.fail("an oversized stream must stop before another read")
            return b"four"

        def close(self) -> None:
            self.closed = True

    body = Body()

    class Client:
        def get_object(self, **_: object) -> object:
            return {"Body": body}

    monkeypatch.setattr(store, "_client", lambda *_: Client())
    with pytest.raises(R2Error, match="exceeds size limit"):
        store.download_file("key", target, expect_bytes=7, max_bytes=7)

    assert body.reads == 2
    assert body.closed
    assert target.read_bytes() == b"known-good"
    assert list(tmp_path.glob(".cache.json.*.part")) == []


def test_operation_timeout_covers_the_object_it_transfers() -> None:
    """A per-attempt read timeout must leave enough room for the object."""

    two_gigabytes = 2_013_155_328
    measured_upload_seconds = 181

    assert object_storage._operation_timeout(None) == 120
    assert object_storage._operation_timeout(0) == 120
    assert object_storage._operation_timeout(two_gigabytes) > measured_upload_seconds


def metadata_store():
    return Boto3R2Store(
        bucket="integration-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "not-logged",
            "R2_SECRET_ACCESS_KEY": "not-logged",
        },
    )


def test_list_returns_keys_and_sizes_across_pages_without_getting_bodies(monkeypatch):
    calls = []

    class Paginator:
        def paginate(self, **kwargs):
            calls.append(kwargs)
            yield {"Contents": [{"Key": "first", "Size": 10}]}
            yield {}
            yield {"Contents": [{"Key": "second", "Size": 20}]}

    class Client:
        def get_paginator(self, name):
            assert name == "list_objects_v2"
            return Paginator()

    store = metadata_store()
    monkeypatch.setattr(store, "_client", lambda: Client())
    assert list(store.iter_objects("scope/")) == [
        ("first", 10),
        ("second", 20),
    ]
    assert calls == [{"Bucket": "integration-test", "Prefix": "scope/"}]


def test_get_metadata_is_from_one_response_and_closes_body(monkeypatch):
    from io import BytesIO

    store = metadata_store()
    body = BytesIO(b"complete")
    metadata = {"sha256": "a" * 64}
    calls = []

    class Client:
        def get_object(self, **kwargs):
            calls.append(kwargs)
            return {
                "Body": body,
                "ETag": '"generation"',
                "ContentLength": 8,
                "Metadata": metadata,
                "ContentType": "application/gzip",
            }

    monkeypatch.setattr(store, "_client", lambda *_: Client())
    assert store.get_bytes_with_metadata("body") == (
        b"complete",
        object_storage.RemoteObject("generation", 8, {"sha256": "a" * 64}, "application/gzip"),
    )
    assert calls == [{"Bucket": "integration-test", "Key": "body"}]
    assert body.closed


@pytest.mark.parametrize("code", ["NoSuchKey", "404", "403", "429"])
def test_get_metadata_only_treats_missing_as_absent(monkeypatch, code):
    store = metadata_store()
    error = ClientError({"Error": {"Code": code, "Message": "private-response"}}, "GetObject")
    calls = []

    class Client:
        def get_object(self, **kwargs):
            calls.append(kwargs)
            raise error

    monkeypatch.setattr(store, "_client", lambda *_: Client())
    if code in {"NoSuchKey", "404"}:
        assert store.get_bytes_with_metadata("body") is None
    else:
        with pytest.raises(R2Error, match=r"^R2 get-object failed$") as caught:
            store.get_bytes_with_metadata("body")
        assert caught.value.__cause__ is error
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["timeout", "metadata"])
def test_get_metadata_failure_closes_stream(monkeypatch, fault):
    store = metadata_store()

    class Body:
        closed = False

        def read(self):
            if fault == "timeout":
                raise ReadTimeoutError(endpoint_url="https://redacted.invalid")
            return b"complete"

        def close(self):
            self.closed = True

    body = Body()

    class Client:
        def get_object(self, **kwargs):
            return {"Body": body}

    monkeypatch.setattr(store, "_client", lambda *_: Client())
    with pytest.raises(R2Error if fault == "timeout" else KeyError):
        store.get_bytes_with_metadata("body")
    assert body.closed


@pytest.mark.parametrize("header", ["foreign-etag", "wrong-size", "matching"])
def test_r2_pinned_sqlite_get_validates_response_before_replacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: str
) -> None:
    import sqlite3
    from contextlib import closing
    from io import BytesIO

    target = tmp_path / "current.sqlite"
    candidate = tmp_path / "candidate.sqlite"
    for path, value in ((target, 10), (candidate, 20)):
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE payload (value int)")
            db.execute("INSERT INTO payload VALUES (?)", (value,))
    old_data, new_data = target.read_bytes(), candidate.read_bytes()
    assert len(old_data) == len(new_data)
    assert old_data != new_data
    calls = []
    body = BytesIO(new_data)

    class Client:
        def get_object(self, **kwargs):
            calls.append(kwargs)
            return {
                "Body": body,
                "ETag": '"foreign"' if header == "foreign-etag" else '"exact"',
                "ContentLength": len(new_data) + (header == "wrong-size"),
            }

    store = metadata_store()
    monkeypatch.setattr(store, "_client", lambda *_: Client())
    if header == "matching":
        store.download_file("once", target, expect_bytes=len(new_data), expect_etag="exact")
        assert target.read_bytes() == new_data
    else:
        with pytest.raises(R2Error, match="pinned identity"):
            store.download_file("once", target, expect_bytes=len(new_data), expect_etag="exact")
        assert target.read_bytes() == old_data
    assert calls == [{"Bucket": store.bucket, "Key": "once", "IfMatch": '"exact"'}]
    assert body.closed
    assert not list(tmp_path.glob("*.part"))
