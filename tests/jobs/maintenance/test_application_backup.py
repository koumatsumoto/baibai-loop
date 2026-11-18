import hashlib

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.r2 import LakeCASConflict, R2Error
from baibai_jobs.maintenance.application_backup import preserve_application


def seeded():
    store = MemoryR2Store()
    store.store_object("baibai.sqlite", b"original application")
    store.store_object("baibai.sqlite.bak-20261118", b"earlier application")
    return store


def preserve(store, tmp_path):
    return preserve_application(
        store, expected_etag=store.head("baibai.sqlite").etag, directory=tmp_path, day="20261118"
    )


def test_same_day_generations_never_replace_existing_backup_and_repeat_reuses(tmp_path):
    store = seeded()
    first = preserve(store, tmp_path)
    assert first.endswith(hashlib.sha256(b"original application").hexdigest())
    assert preserve(store, tmp_path) == first
    assert store.put_keys == [first]
    store.store_object("baibai.sqlite", b"next application")
    second = preserve(store, tmp_path)
    assert first != second
    assert store.values[first].body == b"original application"
    assert store.values[second].body == b"next application"
    assert store.values["baibai.sqlite.bak-20261118"].body == b"earlier application"


@pytest.mark.parametrize(
    "problem",
    [
        "source_race",
        "body_corrupt",
        "existing_corrupt",
        "put_conflict",
        "refused",
        "ack_lost",
        "readback_corrupt",
    ],
)
def test_uncertain_backup_fails_without_overwriting(tmp_path, problem, monkeypatch):
    store = seeded()
    old = store.values["baibai.sqlite.bak-20261118"].body
    key = "baibai.sqlite.bak-20261118-" + hashlib.sha256(b"original application").hexdigest()
    if problem == "existing_corrupt":
        store.store_object(key, b"x" * len(b"original application"))
    download = store.download_file

    def altered_download(name, path, *, expect_bytes=None):
        download(name, path, expect_bytes=expect_bytes)
        if name == "baibai.sqlite" and problem == "source_race":
            store.store_object(name, b"replacement")
        if problem == "body_corrupt" or (problem == "readback_corrupt" and name != "baibai.sqlite"):
            path.write_bytes(b"changed")

    monkeypatch.setattr(store, "download_file", altered_download)
    put = store.put_file

    def altered_put(name, path, **kwargs):
        assert kwargs["if_none_match"] is True
        if problem == "put_conflict":
            store.store_object(name, b"competitor")
        if problem == "refused":
            raise R2Error("refused", http_status=403)
        result = put(name, path, **kwargs)
        if problem == "ack_lost":
            raise R2Error("ack unknown")
        return result

    monkeypatch.setattr(store, "put_file", altered_put)
    with pytest.raises((ValueError, R2Error, LakeCASConflict)):
        preserve(store, tmp_path)
    assert store.values["baibai.sqlite.bak-20261118"].body == old
    assert "baibai.sqlite" not in store.put_keys


def test_wrong_expected_generation_stops_before_get(tmp_path):
    store = seeded()
    with pytest.raises(ValueError, match="changed"):
        preserve_application(store, expected_etag="0" * 32, directory=tmp_path, day="20261118")
    assert not store.get_keys
    assert not store.put_keys
