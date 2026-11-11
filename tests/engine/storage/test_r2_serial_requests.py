"""Finite small-object sessions pace actual HTTP requests without changing bytes."""

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from baibai_engine.storage.objects import r2


@pytest.fixture
def store() -> r2.Boto3R2Store:
    return r2.Boto3R2Store(
        bucket="private-test",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "fake",
            "R2_SECRET_ACCESS_KEY": "fake",
        },
    )


def error(status=429, retry_after="12"):
    return ClientError(
        {
            "Error": {"Code": str(status), "Message": "NEVER_LOG"},
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "HTTPHeaders": {"retry-after": retry_after},
            },
        },
        "GetObject",
    )


def metadata():
    return {"ETag": '"etag"', "ContentLength": 3, "Metadata": {}, "ContentType": "application/json"}


def test_each_http_paced_including_put_verification_and_download(store, monkeypatch, tmp_path):
    clock = [10.0]
    calls = []
    monkeypatch.setattr(r2.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(r2.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    class Client:
        def head_object(self, **kw):
            calls.append(("head", clock[0]))
            return metadata()

        def put_object(self, **kw):
            calls.append(("put", clock[0]))
            return {"ETag": '"etag"'}

        def get_object(self, **kw):
            calls.append(("get", clock[0]))
            return {**metadata(), "Body": BytesIO(b"abc")}

    monkeypatch.setattr(store, "_client", lambda *_: Client())
    payload = tmp_path / "payload"
    payload.write_bytes(b"abc")
    with store.serial_requests(min_interval_seconds=0.5):
        store.put_file(
            "key",
            payload,
            sha256="hash",
            content_md5="md5",
            content_type="application/json",
            if_none_match=True,
        )
        assert store.get_bytes("key") == b"abc"
        assert store.get_bytes_with_metadata("key")[0] == b"abc"
        store.download_file("key", tmp_path / "download", expect_bytes=3)
    assert calls == [("put", 10.0), ("head", 10.5), ("get", 11.0), ("get", 11.5), ("get", 12.0)]
    assert (tmp_path / "download").read_bytes() == b"abc"


@pytest.mark.parametrize("fail", [False, True])
def test_body_lifetime_blocks_next_request_and_exception_releases(store, monkeypatch, fail):
    reading = Event()
    release = Event()
    second_started = Event()
    calls = []
    closed = []

    class Body:
        def read(self):
            reading.set()
            assert release.wait(3)
            if fail:
                raise OSError("body failure")
            return b"abc"

        def close(self):
            closed.append(True)

    class Client:
        def get_object(self, **kw):
            calls.append("get")
            return {"Body": Body()}

        def head_object(self, **kw):
            assert closed
            calls.append("head")
            return metadata()

    monkeypatch.setattr(store, "_client", lambda *_: Client())

    def head():
        second_started.set()
        return store.head("key")

    with store.serial_requests(min_interval_seconds=0.001), ThreadPoolExecutor(2) as pool:
        first = pool.submit(store.get_bytes, "key")
        assert reading.wait(3)
        second = pool.submit(head)
        assert second_started.wait(3)
        assert calls == ["get"]
        release.set()
        if fail:
            with pytest.raises(OSError, match="body failure"):
                first.result(timeout=3)
        else:
            assert first.result(timeout=3) == b"abc"
        assert second.result(timeout=3).etag == "etag"
    assert calls == ["get", "head"]


def test_429_stops_waiter_even_if_worker_exception_swallowed(store, monkeypatch, caplog):
    entered = Event()
    release = Event()
    waiting = Event()
    calls = []

    class Client:
        def get_object(self, **kw):
            calls.append("get")
            entered.set()
            assert release.wait(3)
            raise error()

        def head_object(self, **kw):
            calls.append("head")
            return metadata()

    monkeypatch.setattr(store, "_client", lambda *_: Client())

    def head():
        waiting.set()
        return store.head("PRIVATE_KEY")

    def run_workers():
        with store.serial_requests(min_interval_seconds=0.001), ThreadPoolExecutor(2) as pool:
            first = pool.submit(store.get_bytes, "PRIVATE_KEY")
            assert entered.wait(3)
            second = pool.submit(head)
            assert waiting.wait(3)
            release.set()
            with pytest.raises(r2.R2Error):
                first.result(timeout=3)
            with pytest.raises(r2.R2Error):
                second.result(timeout=3)

    with pytest.raises(r2.R2Error, match="stopped after HTTP 429"):
        run_workers()
    assert calls == ["get"]
    assert "operation=get-object status=429 retry_after=12" in caplog.text
    assert "PRIVATE_KEY" not in caplog.text
    assert "NEVER_LOG" not in caplog.text


@pytest.mark.parametrize(
    ("status", "expected"),
    [(403, r2.R2Error), (500, r2.R2Error), (409, r2.LakeCASConflict), (412, r2.LakeCASConflict)],
)
def test_non429_preserves_translation_and_does_not_stop_scope(store, monkeypatch, status, expected):
    failure = error(status)
    monkeypatch.setattr(
        store,
        "_client",
        lambda *_: SimpleNamespace(head_object=lambda **kw: (_ for _ in ()).throw(failure)),
    )
    with store.serial_requests(min_interval_seconds=0.001):
        for _ in range(2):
            with pytest.raises(expected) as observed:
                store.head("key")
            assert observed.value.__cause__ is failure


def test_client_cache_attempts_and_default_are_separate(store, monkeypatch):
    configurations = []

    def client(*a, **kw):
        configurations.append(kw["config"].retries)
        return object()

    monkeypatch.setattr(r2.boto3.session, "Session", lambda: SimpleNamespace(client=client))
    default = store._client()
    with store.serial_requests(min_interval_seconds=0.1):
        serial = store._client()
        assert serial is not default
        assert store._client() is serial
    assert store._client() is default
    assert [c["total_max_attempts"] for c in configurations] == [3, 1]


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan")])
def test_invalid_intervals_rejected(store, interval):
    with (
        pytest.raises(ValueError, match="positive and finite"),
        store.serial_requests(min_interval_seconds=interval),
    ):
        pass


def test_nested_scope_rejected_and_cleanup(store):
    with store.serial_requests(min_interval_seconds=0.1):
        with (
            pytest.raises(ValueError, match="cannot overlap"),
            store.serial_requests(min_interval_seconds=0.1),
        ):
            pass
        with pytest.raises(ValueError, match="not listing"):
            list(store.iter_objects("prefix"))
    with pytest.raises(RuntimeError), store.serial_requests(min_interval_seconds=0.1):
        raise RuntimeError()
    assert store._serial is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12", 12),
        ("Wed, 11 Nov 2026 03:00:00 GMT", "2026-11-11T03:00:00+00:00"),
        ("SECRET\nheader", "invalid"),
        ("x" * 200, "invalid"),
        (None, None),
    ],
)
def test_retry_after_safe_normalization(raw, expected):
    assert r2._retry_after(error(retry_after=raw)) == expected


def test_spacing_includes_client_setup_and_body_time(store, monkeypatch):
    clock = [1.0]
    sent = []
    monkeypatch.setattr(r2.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(r2.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    class Body(BytesIO):
        def close(self):
            clock[0] += 2
            super().close()

    class Client:
        def get_object(self, **kw):
            sent.append(clock[0])
            return {"Body": Body(b"abc")}

    def client(*_):
        if not sent:
            clock[0] += 10
        return Client()

    monkeypatch.setattr(store, "_client", client)
    with store.serial_requests(min_interval_seconds=0.5):
        store.get_bytes("key")
        store.get_bytes("key")
    assert sent == [11.0, 13.5]
