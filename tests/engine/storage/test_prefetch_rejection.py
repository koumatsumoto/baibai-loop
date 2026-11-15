"""Prefetch cannot turn a verified R2 refusal into a second sequential request."""

import threading
from contextlib import closing, nullcontext
from io import BytesIO
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from baibai_engine.storage.objects import r2
from baibai_engine.storage.snapshots.models import LakeObject
from baibai_engine.storage.snapshots.objects import (
    LakeObjectError,
    LakeObjectRejected,
    R2ObjectSource,
    R2ReadCredentials,
)
from baibai_engine.storage.snapshots.prefetch import PrefetchingSource


def _source(tmp_path, monkeypatch, *, status, code, strict=False):
    calls = []

    def get_object(**kwargs):
        calls.append(kwargs["Key"])
        if len(calls) == 1:
            metadata = {} if status is None else {"HTTPStatusCode": status}
            raise ClientError(
                {"Error": {"Code": code, "Message": "PRIVATE"}, "ResponseMetadata": metadata},
                "GetObject",
            )
        return {"Body": BytesIO(b"ok")}

    client = SimpleNamespace(get_object=get_object, close=lambda: None)
    monkeypatch.setattr(
        r2.boto3.session,
        "Session",
        lambda: SimpleNamespace(client=lambda *args, **kwargs: client),
    )
    monkeypatch.setenv("R2_STOP_ON_REJECTION", "1" if strict else "0")
    monkeypatch.delenv("R2_REJECTION_STOP_FILE", raising=False)
    credentials = R2ReadCredentials("a" * 32, "A" * 20, "fixture-only", "private-test")
    base = R2ObjectSource(credentials)
    key = "lake/l1/source/test/object.parquet"
    source = PrefetchingSource(
        base=base,
        objects=[
            LakeObject(
                key=key,
                sha256="a" * 64,
                bytes=2,
                rows=1,
                min_key=("1",),
                max_key=("1",),
            )
        ],
        source_factory=lambda: closing(R2ObjectSource(credentials)),
        mirror=tmp_path / "mirror",
        workers=1,
        buffer_slots=1,
        wait_seconds=1.0,
    )
    return source, base, key, calls


@pytest.mark.parametrize("status", [403, 429])
def test_default_prefetch_rejection_does_not_retry_on_the_base_source(
    tmp_path, monkeypatch, capsys, status
):
    source, base, key, calls = _source(tmp_path, monkeypatch, status=status, code=str(status))
    try:
        for _ in range(2):
            with pytest.raises(LakeObjectError, match=f"HTTP {status}"):
                source.read_bytes(key)
        with pytest.raises(LakeObjectError, match=f"HTTP {status}"):
            source.download_file(key, tmp_path / "download", max_bytes=2)
    finally:
        source.close()
        base.close()
    assert calls == [key]
    assert not (tmp_path / "download").exists()
    output = capsys.readouterr()
    assert "PRIVATE" not in output.err
    assert "reads continue sequentially" not in output.err


@pytest.mark.parametrize("status", [403, 429])
def test_strict_rejection_is_propagated_from_worker_without_a_fallback(
    tmp_path, monkeypatch, status
):
    # Even without a cross-process stop file, one hydration must not lose the
    # worker's BaseException and retry through the separate base store.
    source, base, key, calls = _source(
        tmp_path, monkeypatch, status=status, code=str(status), strict=True
    )
    try:
        with pytest.raises(r2.R2OperationStopped):
            source.read_bytes(key)
    finally:
        source.close()
        base.close()
    assert calls == [key]


@pytest.mark.parametrize(("status", "code"), [(503, "503"), (None, "403"), (None, "429")])
def test_transient_or_unverified_status_keeps_the_existing_sequential_fallback(
    tmp_path, monkeypatch, status, code
):
    source, base, key, calls = _source(tmp_path, monkeypatch, status=status, code=code)
    try:
        assert source.read_bytes(key) == b"ok"
    finally:
        source.close()
        base.close()
    assert calls == [key, key]


def test_rejection_stops_unreserved_keys_before_wind_down(tmp_path):
    second_started = threading.Event()
    finish_second = threading.Event()
    terminal_set = threading.Event()
    allow_wind_down = threading.Event()
    attempted_next = threading.Event()
    calls = []
    reservations_after_terminal = []
    keys = [f"lake/l1/source/test/{name}.parquet" for name in ("a", "b", "c")]

    class Source:
        def read_bytes(self, key):
            calls.append(key)
            if key == keys[0]:
                assert second_started.wait(5)
                raise LakeObjectRejected("lake object request rejected: HTTP 429")
            if key == keys[1]:
                second_started.set()
                assert finish_second.wait(5)
            return b"ok"

    class ScheduledSource(PrefetchingSource):
        def _wind_down(self):
            if threading.current_thread().name.startswith("lake-prefetch"):
                # Pause after the refusal was recorded, before normal wind-down.
                terminal_set.set()
                assert allow_wind_down.wait(5)
            super()._wind_down()

        def _take(self):
            taken = super()._take()
            if terminal_set.is_set():
                reservations_after_terminal.append(taken)
                attempted_next.set()
            return taken

    source = ScheduledSource(
        base=Source(),
        objects=[
            LakeObject(key=key, sha256="a" * 64, bytes=2, rows=1, min_key=("1",), max_key=("1",))
            for key in keys
        ],
        source_factory=lambda: nullcontext(Source()),
        mirror=tmp_path / "mirror",
        workers=2,
        buffer_slots=3,
    )
    try:
        assert terminal_set.wait(5)
        assert source._terminal_error is not None
        assert source._next_index == 2
        finish_second.set()
        assert attempted_next.wait(5)
        assert reservations_after_terminal == [None]
    finally:
        allow_wind_down.set()
        finish_second.set()
        source.close()
    assert set(calls) == set(keys[:2])
