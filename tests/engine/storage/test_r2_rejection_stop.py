"""Explicit recovery passes stop on verified rejections, including across processes."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from baibai_engine.storage.objects import r2


def configuration(tmp_path):
    return {
        "R2_ACCOUNT_ID": "a" * 32,
        "R2_ACCESS_KEY_ID": "fake",
        "R2_SECRET_ACCESS_KEY": "fake",
        "R2_STOP_ON_REJECTION": "1",
        "R2_REJECTION_STOP_FILE": str(tmp_path / "stopped"),
    }


def failure(status, code="SECRET"):
    return ClientError(
        {
            "Error": {"Code": code, "Message": "PRIVATE"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "GetObject",
    )


@pytest.mark.parametrize("status", [403, "403", 429, "429"])
@pytest.mark.parametrize("operation", ["head", "get", "metadata", "download", "put", "list"])
def test_verified_rejection_aborts_all_paths(tmp_path, monkeypatch, status, operation):
    env = configuration(tmp_path)
    store = r2.Boto3R2Store(bucket="private", env=env)
    calls = []

    def reject(**kwargs):
        calls.append(kwargs)
        raise failure(status)

    def pages(**kwargs):
        reject(**kwargs)
        yield {}

    client = SimpleNamespace(
        head_object=reject,
        get_object=reject,
        put_object=reject,
        get_paginator=lambda _: SimpleNamespace(paginate=pages),
    )
    monkeypatch.setattr(store, "_client", lambda *_: client)
    source = tmp_path / "source"
    source.write_bytes(b"abc")
    actions = {
        "head": lambda: store.head("private-key"),
        "get": lambda: store.get_bytes("private-key"),
        "metadata": lambda: store.get_bytes_with_metadata("private-key"),
        "download": lambda: store.download_file("private-key", tmp_path / "download"),
        "put": lambda: store.put_file(
            "private-key", source, sha256="sha", content_md5="md5", content_type="application/json"
        ),
        "list": lambda: list(store.iter_objects("private-prefix")),
    }

    def ordinary_error_handler():
        try:
            actions[operation]()
        except Exception:
            pytest.fail("ordinary per-source error handling swallowed a fatal rejection")

    with pytest.raises(r2.R2OperationStopped) as caught:
        ordinary_error_handler()
    assert str(status) in str(caught.value)
    assert caught.value.__suppress_context__
    assert "PRIVATE" not in str(caught.value)
    assert Path(env["R2_REJECTION_STOP_FILE"]).read_text() == f"{status}\n"
    assert Path(env["R2_REJECTION_STOP_FILE"]).stat().st_mode & 0o777 == 0o600
    assert not (tmp_path / "download").exists()
    for action in actions.values():
        with pytest.raises(r2.R2OperationStopped, match="previously stopped"):
            action()
    assert len(calls) == 1
    other = r2.Boto3R2Store(bucket="private", env=env)
    with pytest.raises(r2.R2OperationStopped, match="previously stopped"):
        other.head("different-key")


@pytest.mark.parametrize("status", [None, [], True, "429 PRIVATE", 500, 404, 412])
def test_no_status_inference_or_changed_error_translation(tmp_path, monkeypatch, status):
    store = r2.Boto3R2Store(bucket="private", env=configuration(tmp_path))
    calls = []

    def reject(**kwargs):
        calls.append(kwargs)
        raise failure(status, code="429")

    monkeypatch.setattr(store, "_client", lambda *_: SimpleNamespace(head_object=reject))
    for _ in range(2):
        with pytest.raises(r2.LakePublishError):
            store.head("key")
    assert len(calls) == 2
    assert not (tmp_path / "stopped").exists()


def test_strict_retry_budget_and_serial_scope(tmp_path, monkeypatch):
    store = r2.Boto3R2Store(bucket="private", env=configuration(tmp_path))
    budgets = []

    def client(*args, **kwargs):
        budgets.append(kwargs["config"].retries["total_max_attempts"])
        return SimpleNamespace(head_object=lambda **_: (_ for _ in ()).throw(failure(403)))

    monkeypatch.setattr(r2.boto3.session, "Session", lambda: SimpleNamespace(client=client))
    store._client()
    with (
        store.serial_requests(min_interval_seconds=0.001),
        pytest.raises(r2.R2OperationStopped),
    ):
        store.head("key")
    assert budgets == [1]
    with pytest.raises(r2.R2OperationStopped):
        store.head("key")


def test_new_child_observes_stop_before_network(tmp_path):
    env = configuration(tmp_path)
    Path(env["R2_REJECTION_STOP_FILE"]).write_text("429\n")
    script = """
import json, sys
from baibai_engine.storage.objects.r2 import Boto3R2Store, R2OperationStopped
try:
    Boto3R2Store(bucket='private', env=json.loads(sys.argv[1])).head('key')
except R2OperationStopped:
    sys.exit(75)
sys.exit(1)
"""
    import json

    result = subprocess.run([sys.executable, "-c", script, json.dumps(env)], capture_output=True)
    assert result.returncode == 75
    assert result.stdout == result.stderr == b""


def test_stop_persistence_failure_remains_fatal(tmp_path, monkeypatch):
    store = r2.Boto3R2Store(bucket="private", env=configuration(tmp_path))
    monkeypatch.setattr(
        store,
        "_client",
        lambda *_: SimpleNamespace(head_object=lambda **_: (_ for _ in ()).throw(failure(429))),
    )
    monkeypatch.setattr(r2.os, "open", lambda *a, **kw: (_ for _ in ()).throw(OSError("PRIVATE")))
    with pytest.raises(r2.R2OperationStopped, match="could not be persisted"):
        store.head("key")
    with pytest.raises(r2.R2OperationStopped, match="previously stopped"):
        store.head("key")


@pytest.mark.parametrize(
    "settings",
    [
        {"R2_STOP_ON_REJECTION": "yes"},
        {"R2_STOP_ON_REJECTION": "0"},
        {"R2_REJECTION_STOP_FILE": "relative"},
        {"R2_REJECTION_STOP_FILE": "/does-not-exist/stopped"},
    ],
)
def test_malformed_configuration_rejected(tmp_path, settings):
    with pytest.raises(ValueError, match="R2 rejection stop"):
        r2.Boto3R2Store(bucket="private", env={**configuration(tmp_path), **settings})
