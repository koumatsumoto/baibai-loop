"""実R2受入は専用bucketで、自分が作成したkeyだけを掃除する。"""

from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from tests.integration import test_lake_r2_acceptance as acceptance

from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, RemoteObject
from baibai_engine.storage.snapshots import objects as lake_objects
from baibai_engine.storage.snapshots.keys import current_release_pointer_key


def test_parallel_acceptance_is_rejected_before_store_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.delenv("R2_LAKE_ACCEPTANCE_BUCKET", raising=False)
    with pytest.raises(pytest.fail.Exception, match="serial execution: add -n 0"):
        acceptance._store()


@pytest.mark.parametrize("bucket", ["baibai-stores", "ordinary-bucket"])
def test_acceptance_rejects_other_buckets_before_creating_a_client(monkeypatch, bucket):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setenv("R2_LAKE_ACCEPTANCE_BUCKET", bucket)
    monkeypatch.setattr(
        acceptance,
        "Boto3R2Store",
        lambda **_: pytest.fail("invalid acceptance bucket reached the client"),
    )
    with pytest.raises(pytest.fail.Exception, match="dedicated non-production bucket"):
        acceptance._store()


def test_acceptance_passes_only_the_explicit_bucket_to_its_client(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.setenv("R2_LAKE_ACCEPTANCE_BUCKET", "baibai-lake-acceptance")
    calls = []
    client = object()

    def store(**kwargs):
        calls.append(kwargs)
        return client

    monkeypatch.setattr(acceptance, "Boto3R2Store", store)
    assert acceptance._store() is client
    assert calls == [{"bucket": "baibai-lake-acceptance"}]


@pytest.fixture
def stub_r2(monkeypatch):
    remote = Mock(spec=acceptance.Boto3R2Store)
    remote.bucket = "baibai-lake-acceptance"
    remote._request.side_effect = lambda _: nullcontext()
    objects: dict[str, bytes] = {}

    def put(key, path, *, if_none_match, **_):
        if if_none_match and key in objects:
            raise LakeCASConflict("key already exists")
        objects[key] = payload = path.read_bytes()
        return RemoteObject(etag="new-generation", size=len(payload), metadata={}, content_type="")

    def delete(*, Bucket, Delete):
        assert Bucket == remote.bucket
        for item in Delete["Objects"]:
            objects.pop(item["Key"], None)
        return {"Deleted": Delete["Objects"]}

    remote.put_file.side_effect = put
    remote.get_bytes.side_effect = objects.__getitem__
    remote._client.return_value.delete_objects.side_effect = delete
    monkeypatch.setattr(acceptance, "_store", lambda: remote)
    return remote, objects


def _write(store, key, path, *, if_none_match=True, if_match=None):
    return store.put_file(
        key,
        path,
        sha256="a" * 64,
        content_md5="md5",
        content_type="application/json",
        if_none_match=if_none_match,
        if_match=if_match,
    )


def test_acceptance_cleanup_leaves_preexisting_and_late_keys(stub_r2, tmp_path):
    remote, objects = stub_r2
    pointer = current_release_pointer_key()
    occupied = "lake/pointers/other/current.json"
    payload = tmp_path / "body"
    payload.write_bytes(b"this-case")
    objects[pointer] = b"preexisting-unscoped"

    with acceptance._isolated_store() as other, acceptance._isolated_store() as store:
        owned = store.remote_key(pointer)
        other_key = other.remote_key(pointer)
        occupied_key = store.remote_key(occupied)
        assert owned != other_key
        objects[occupied_key] = b"already-present-in-same-prefix"
        with pytest.raises(pytest.fail.Exception, match="mutually exclusive"):
            _write(store, occupied, payload, if_match="other-generation")
        with pytest.raises(pytest.fail.Exception, match="must create a key"):
            _write(store, occupied, payload, if_none_match=False, if_match="other-generation")
        remote.put_file.assert_not_called()
        with pytest.raises(LakeCASConflict, match="already exists"):
            _write(store, occupied, payload)

        _write(store, pointer, payload)
        assert store.get_bytes(pointer) == b"this-case"
        objects[other_key] = b"late-independent-writer"
        late_in_same_prefix = store.remote_key("lake/late.json")
        objects[late_in_same_prefix] = b"late-unowned-key"

    remote._client.return_value.delete_objects.assert_called_once_with(
        Bucket=remote.bucket, Delete={"Objects": [{"Key": owned}]}
    )
    assert objects == {
        pointer: b"preexisting-unscoped",
        occupied_key: b"already-present-in-same-prefix",
        other_key: b"late-independent-writer",
        late_in_same_prefix: b"late-unowned-key",
    }


def test_acceptance_does_not_delete_a_write_without_acknowledgement(stub_r2, tmp_path):
    remote, objects = stub_r2
    put = remote.put_file.side_effect

    def put_without_reply(*args, **kwargs):
        put(*args, **kwargs)
        raise LakePublishError("response lost")

    remote.put_file.side_effect = put_without_reply
    pointer = current_release_pointer_key()
    payload = tmp_path / "body"
    payload.write_bytes(b"unknown-commit")
    with (
        acceptance._isolated_store() as store,
        pytest.raises(LakePublishError, match="response lost"),
    ):
        _write(store, pointer, payload)

    remote._client.assert_not_called()
    assert objects == {store.remote_key(pointer): b"unknown-commit"}


def test_acceptance_cleanup_requires_per_key_acknowledgement(stub_r2, tmp_path):
    remote, _objects = stub_r2
    remote._client.return_value.delete_objects.side_effect = None
    remote._client.return_value.delete_objects.return_value = {"Deleted": []}
    payload = tmp_path / "body"
    payload.write_bytes(b"not-deleted")
    with (
        pytest.raises(pytest.fail.Exception, match="cleanup was not acknowledged"),
        acceptance._isolated_store() as store,
    ):
        _write(store, current_release_pointer_key(), payload)


def test_acceptance_reader_uses_the_same_scoped_key(stub_r2, tmp_path, monkeypatch):
    remote, objects = stub_r2
    monkeypatch.delenv("R2_STOP_ON_REJECTION", raising=False)
    monkeypatch.delenv("R2_REJECTION_STOP_FILE", raising=False)
    pointer = current_release_pointer_key()
    objects[pointer] = b"unscoped-pointer"
    payload = tmp_path / "body"
    payload.write_bytes(b"isolated-pointer")
    with acceptance._isolated_store() as store:
        _write(store, pointer, payload)
        env = {
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "a" * 16,
            "R2_SECRET_ACCESS_KEY": "synthetic-secret",
        }
        reader_factory = Mock(return_value=remote)
        monkeypatch.setattr(acceptance, "Boto3R2Store", reader_factory)
        monkeypatch.setattr(lake_objects, "Boto3R2Store", store.reader_store)
        with lake_objects.open_lake(mirror=tmp_path / "reader", bucket=store.bucket, env=env) as (
            _session,
            cache,
        ):
            assert cache.source.read_bytes(pointer) == b"isolated-pointer"
        reader_factory.assert_called_once_with(bucket=store.bucket, env=env)
        remote.get_bytes.assert_called_once_with(store.remote_key(pointer))
