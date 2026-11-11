"""SDK failure diagnostics expose status and local correlation, never request data."""

import hashlib
from types import SimpleNamespace

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from baibai_engine.storage.objects import r2

KEY = "PRIVATE_KEY/原本?token=PRIVATE_TOKEN"
SECRETS = (
    KEY,
    "PRIVATE_BUCKET",
    "PRIVATE_MESSAGE",
    "PRIVATE_HEADER",
    "PRIVATE_REQUEST_ID",
    "PRIVATE_OPERATION",
    "PRIVATE_ACCESS",
    "PRIVATE_SECRET",
    "PRIVATE_ENDPOINT",
)


@pytest.fixture
def store():
    return r2.Boto3R2Store(
        bucket="PRIVATE_BUCKET",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "PRIVATE_ACCESS",
            "R2_SECRET_ACCESS_KEY": "PRIVATE_SECRET",
        },
    )


def failure(status=403, code="AccessDenied"):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "PRIVATE_MESSAGE"},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "HTTPHeaders": {"x-secret": "PRIVATE_HEADER"},
                "RequestId": "PRIVATE_REQUEST_ID",
            },
        },
        "PRIVATE_OPERATION",
    )


def install_failure(store, monkeypatch, error):
    calls = []

    def fail(operation):
        def invoke(**kwargs):
            calls.append((operation, kwargs))
            raise error

        return invoke

    client = SimpleNamespace(
        head_object=fail("head-object"),
        get_object=fail("get-object"),
        put_object=fail("put-object"),
        get_paginator=lambda _: SimpleNamespace(paginate=fail("list-objects")),
    )
    monkeypatch.setattr(store, "_client", lambda *_: client)
    return calls


def invoke(store, method, tmp_path):
    if method == "download_file":
        return store.download_file(KEY, tmp_path / "download")
    if method == "put_file":
        source = tmp_path / "upload"
        source.write_bytes(b"fixture")
        return store.put_file(
            KEY,
            source,
            sha256="hash",
            content_md5="md5",
            content_type="application/json",
            if_none_match=True,
        )
    if method == "iter_objects":
        return list(store.iter_objects(KEY))
    return getattr(store, method)(KEY)


def assert_diagnostic(caplog, operation, status, *, key=KEY):
    records = [r for r in caplog.records if r.message.startswith("R2 request failed:")]
    assert len(records) == 1
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest() if key is not None else "none"
    assert (
        records[0].message
        == f"R2 request failed: operation={operation} status={status} key_sha256={digest}"
    )
    assert records[0].exc_info is None
    assert records[0].stack_info is None
    for secret in SECRETS:
        assert secret not in caplog.text
        assert secret not in str(records[0].__dict__)


@pytest.mark.parametrize(
    ("method", "operation"),
    [
        ("head", "head-object"),
        ("get_bytes", "get-object"),
        ("get_bytes_with_metadata", "get-object"),
        ("download_file", "get-object"),
        ("put_file", "put-object"),
        ("iter_objects", "list-objects"),
    ],
)
@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_actual_operation_and_http_status_are_logged_once(
    store, monkeypatch, tmp_path, caplog, method, operation, status
):
    error = failure(status)
    calls = install_failure(store, monkeypatch, error)
    with pytest.raises(r2.R2Error) as caught:
        invoke(store, method, tmp_path)
    assert caught.value.__cause__ is error
    assert str(caught.value) == f"R2 {operation} failed"
    assert len(calls) == 1
    assert calls[0][0] == operation
    assert_diagnostic(caplog, operation, status, key=None if method == "iter_objects" else KEY)


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("PreconditionFailed", 412),
        ("ConditionalRequestConflict", 409),
        ("unknown", 412),
        ("unknown", 409),
        (412, None),
    ],
)
def test_conditional_failure_preserves_type_cause_and_request(
    store, monkeypatch, tmp_path, caplog, code, status
):
    error = failure(status, code)
    calls = install_failure(store, monkeypatch, error)
    with pytest.raises(r2.LakeCASConflict) as caught:
        invoke(store, "put_file", tmp_path)
    assert caught.value.__cause__ is error
    assert calls[0][1]["IfNoneMatch"] == "*"
    assert len(calls) == 1
    assert_diagnostic(caplog, "put-object", "unknown" if status is None else status)


@pytest.mark.parametrize(
    "method",
    ["head", "get_bytes", "get_bytes_with_metadata", "download_file", "put_file", "iter_objects"],
)
@pytest.mark.parametrize(
    "error_type", [ConnectTimeoutError, ReadTimeoutError, EndpointConnectionError]
)
def test_transport_failures_have_unknown_status(
    store, monkeypatch, tmp_path, caplog, method, error_type
):
    error = error_type(endpoint_url="https://PRIVATE_ENDPOINT")
    calls = install_failure(store, monkeypatch, error)
    with pytest.raises(r2.R2Error) as caught:
        invoke(store, method, tmp_path)
    assert caught.value.__cause__ is error
    assert len(calls) == 1
    operation = calls[0][0]
    assert_diagnostic(caplog, operation, "unknown", key=None if method == "iter_objects" else KEY)


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        "PRIVATE_HEADER",
        {},
        {"HTTPStatusCode": None},
        {"HTTPStatusCode": True},
        {"HTTPStatusCode": 429.0},
        {"HTTPStatusCode": []},
        {"HTTPStatusCode": {}},
        {"HTTPStatusCode": 99},
        {"HTTPStatusCode": 600},
        {"HTTPStatusCode": " 429"},
        {"HTTPStatusCode": "429\nPRIVATE_HEADER"},
        {"HTTPStatusCode": "HTTP 429"},
        {"HTTPStatusCode": "４２９"},
    ],
)
def test_malformed_status_is_unknown_without_parsing_service_message(
    store, monkeypatch, caplog, metadata
):
    error = failure(code="429")
    error.response["ResponseMetadata"] = metadata
    install_failure(store, monkeypatch, error)
    with pytest.raises(r2.R2Error) as caught:
        store.head(KEY)
    assert caught.value.__cause__ is error
    assert_diagnostic(caplog, "head-object", "unknown")


@pytest.mark.parametrize("status", [403, "403", 429, "429", 503, "503"])
def test_canonical_integer_or_ascii_status_only(store, monkeypatch, caplog, status):
    install_failure(store, monkeypatch, failure(status))
    with pytest.raises(r2.R2Error):
        store.head(KEY)
    assert_diagnostic(caplog, "head-object", int(status))


@pytest.mark.parametrize("details", [None, [], "PRIVATE_MESSAGE", {"Code": []}, {"Code": {}}])
def test_malformed_error_details_do_not_hide_http_status(store, monkeypatch, caplog, details):
    error = failure()
    error.response["Error"] = details
    install_failure(store, monkeypatch, error)
    with pytest.raises(r2.R2Error) as caught:
        store.head(KEY)
    assert caught.value.__cause__ is error
    assert_diagnostic(caplog, "head-object", 403)


@pytest.mark.parametrize("method", ["head", "get_bytes_with_metadata"])
def test_expected_absence_does_not_log_failed_request(store, monkeypatch, caplog, method):
    install_failure(store, monkeypatch, failure(404, "NoSuchKey"))
    assert getattr(store, method)(KEY) is None
    assert not caplog.records


def test_failed_put_verification_names_head_not_put(store, monkeypatch, tmp_path, caplog):
    error = failure(403)
    calls = []

    def put(**kw):
        calls.append("put")
        return {"ETag": '"generation"'}

    def head(**kw):
        calls.append("head")
        raise error

    monkeypatch.setattr(
        store, "_client", lambda *_: SimpleNamespace(put_object=put, head_object=head)
    )
    with pytest.raises(r2.R2Error) as caught:
        invoke(store, "put_file", tmp_path)
    assert caught.value.__cause__ is error
    assert calls == ["put", "head"]
    assert_diagnostic(caplog, "head-object", 403)


def test_missing_put_verification_does_not_expose_raw_key(store, monkeypatch, tmp_path):
    monkeypatch.setattr(
        store,
        "_client",
        lambda *_: SimpleNamespace(put_object=lambda **kw: {"ETag": '"generation"'}),
    )
    monkeypatch.setattr(store, "head", lambda _: None)
    with pytest.raises(r2.R2Error, match=r"^R2 object missing after successful put$"):
        invoke(store, "put_file", tmp_path)
