"""R2 transfer bounds and conditional errors belong to the object adapter."""

from __future__ import annotations

from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

from baibai_engine.storage.objects import r2 as object_storage
from baibai_engine.storage.objects.r2 import Boto3R2Store, LakeCASConflict, LakePublishError, R2Error


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

    assert requests[0]["IfMatch"] == "start-etag"
    assert "IfNoneMatch" not in requests[0]
    assert requests[0]["ContentMD5"] == "md5"


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
