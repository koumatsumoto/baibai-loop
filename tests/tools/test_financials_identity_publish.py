"""架空のstoreだけで、固定snapshotとCAS・応答不明・lease境界を確認する。"""

import hashlib
import json
from dataclasses import replace
from datetime import timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.tools.test_financials_identity import AT, legacy_case, prepare
from tools import financials_identity_publish as publisher

from baibai_engine.storage.objects.r2 import LakeCASConflict, R2Error
from baibai_jobs.publication.lease import LEASE_TTL, LeaseError, acquire

BUCKET = "synthetic-only"


class CutoverStore(MemoryR2Store):
    def __init__(self):
        super().__init__()
        self.fault_key = None
        self.fault = None
        self.requests = []
        self.directory = None

    def put_file(self, key, path, **kwargs):
        if key in publisher.ORDER:
            # digest/bytesはリモート変更より先に永続fileとして存在する。
            assert self.directory is not None
            assert path == self.directory / key
            assert hashlib.sha256(path.read_bytes()).hexdigest() == kwargs["sha256"]
            assert (self.directory / "plan.json").is_file()
            if key == publisher.MACHINE:
                assert (self.directory / "machine-upload.json").is_file()
            self.requests.append((key, kwargs["if_match"], kwargs["sha256"]))
            if self.fault_key == key and self.fault == "before":
                self.fault = None
                raise R2Error("synthetic timeout before PUT")
            if self.fault_key == key and self.fault == "conflict":
                self.fault = None
                self.store_object(key, b"another writer")
                raise LakeCASConflict("synthetic racing writer")
        response = super().put_file(key, path, **kwargs)
        if self.fault_key == key and self.fault == "after":
            self.fault = None
            raise R2Error("synthetic lost acknowledgement")
        return response


def fixture(tmp_path, monkeypatch):
    case, _ = legacy_case(tmp_path, monkeypatch)
    converted = tmp_path / "converted"
    prepare(case, converted)
    store = CutoverStore()
    for key, path in (("runs.sqlite", case.runs), ("baibai.sqlite", case.app)):
        store.store_object(key, path.read_bytes(), content_type="application/octet-stream")
    for key in publisher.UNTOUCHED:
        store.store_object(key, f"original synthetic {key}".encode())
    manifest = {"schema_version": 1, "written_at_utc": AT.isoformat(), "keys": {
        key: store.head(key).etag for key in (*publisher.UNTOUCHED, "runs.sqlite")
    }}
    store.store_object(publisher.MACHINE, json.dumps(manifest).encode())
    generations = tmp_path / "paired"
    generations.mkdir()
    for key in (*publisher.UNTOUCHED, "runs.sqlite", "baibai.sqlite"):
        (generations / f"{key}.etag").write_text('"' + store.head(key).etag + '"')
    handle, _ = acquire(store, "operator", now=AT)
    directory = tmp_path / "frozen"
    store.directory = directory
    kwargs = dict(
        bucket=BUCKET, source_app=case.app, source_runs=case.runs, copy=converted,
        generations=generations, directory=directory, source_revision="a" * 40,
        target_revision="b" * 40, handle=handle, writers_drained=True, clock=lambda: AT,
    )
    return case, store, kwargs


def apply(store, kwargs, **changes):
    arguments = {key: kwargs[key] for key in (
        "bucket", "directory", "handle", "writers_drained", "clock"
    )}
    return publisher.publish_upload(store, **{**arguments, **changes})


def test_fixed_snapshot_put_order_cas_backups_and_verified_noop(tmp_path, monkeypatch):
    case, store, kwargs = fixture(tmp_path, monkeypatch)
    source = (case.app.read_bytes(), case.runs.read_bytes())
    before = {key: value.body for key, value in store.values.items()}
    publisher.prepare_upload(store, **kwargs)
    assert not store.requests
    directory = kwargs["directory"]
    assert (directory / "backup" / "baibai.sqlite").read_bytes() == source[0]
    assert (directory / "backup" / "runs.sqlite").read_bytes() == source[1]
    assert (directory / "backup" / publisher.MACHINE).read_bytes() == before[publisher.MACHINE]
    plan = publisher._load(directory, BUCKET)
    for key, digest in plan.uploads.items():
        assert publisher._descriptor(directory / key) == digest
    result = apply(store, kwargs)
    assert result["next_key"] is None
    assert [item[0] for item in store.requests] == list(publisher.ORDER)
    assert [item[1] for item in store.requests] == [plan.base_etags[key] for key in publisher.ORDER]
    machine = json.loads(store.get_bytes(publisher.MACHINE))
    assert machine["keys"]["runs.sqlite"] == store.head("runs.sqlite").etag
    for key in publisher.UNTOUCHED:
        assert store.values[key].body == before[key]
    assert (case.app.read_bytes(), case.runs.read_bytes()) == source
    assert apply(store, kwargs) == result
    assert len(store.requests) == 3


@pytest.mark.parametrize("key", publisher.ORDER)
@pytest.mark.parametrize("when", ["before", "after"])
def test_uncertain_put_is_read_before_resend_and_original_clock_stays_fixed(
    tmp_path, monkeypatch, key, when
):
    _, store, kwargs = fixture(tmp_path, monkeypatch)
    publisher.prepare_upload(store, **kwargs)
    store.fault_key, store.fault = key, when
    with pytest.raises(R2Error, match="synthetic"):
        apply(store, kwargs)
    attempts = len([request for request in store.requests if request[0] == key])
    frozen = kwargs["directory"] / publisher.MACHINE
    original_machine_upload = frozen.read_bytes() if frozen.exists() else None
    store.get_keys.clear()
    result = apply(store, kwargs, clock=lambda: AT + timedelta(minutes=1))
    assert result["next_key"] is None
    assert len([request for request in store.requests if request[0] == key]) == attempts + (when == "before")
    assert key in store.get_keys
    assert store.get_bytes(publisher.MACHINE) == frozen.read_bytes()
    if original_machine_upload is not None:
        assert frozen.read_bytes() == original_machine_upload


@pytest.mark.parametrize("mode", ["paired", "source", "manifest", "drain", "purpose"])
def test_prepare_rejects_unbound_sources_without_store_put(tmp_path, monkeypatch, mode):
    case, store, kwargs = fixture(tmp_path, monkeypatch)
    if mode == "paired":
        (kwargs["generations"] / "runs.sqlite.etag").write_text("stale-pull")
    elif mode == "source":
        # ETagを新HEADへ付け替えても、元DBのbytesと結び付かない。
        store.store_object("runs.sqlite", b"different source")
        (kwargs["generations"] / "runs.sqlite.etag").write_text(store.head("runs.sqlite").etag)
        manifest = json.loads(store.get_bytes(publisher.MACHINE))
        manifest["keys"]["runs.sqlite"] = store.head("runs.sqlite").etag
        store.store_object(publisher.MACHINE, json.dumps(manifest).encode())
    elif mode == "manifest":
        store.store_object(publisher.MACHINE, b"{}")
    elif mode == "drain":
        kwargs["writers_drained"] = False
    else:
        kwargs["handle"] = replace(kwargs["handle"], purpose="daily")
    before = (case.app.read_bytes(), case.runs.read_bytes())
    with pytest.raises(ValueError, match="differs|requires"):
        publisher.prepare_upload(store, **kwargs)
    assert not store.requests
    assert before == (case.app.read_bytes(), case.runs.read_bytes())


def test_expired_handle_and_reacquired_lease_only_resume_known_prefix(tmp_path, monkeypatch):
    _, store, kwargs = fixture(tmp_path, monkeypatch)
    publisher.prepare_upload(store, **kwargs)
    store.fault_key, store.fault = publisher.MACHINE, "before"
    with pytest.raises(R2Error, match="synthetic"):
        apply(store, kwargs)
    requests = len(store.requests)
    later = AT + LEASE_TTL
    with pytest.raises(LeaseError, match="expired"):
        apply(store, kwargs, clock=lambda: later)
    assert len(store.requests) == requests
    newer, _ = acquire(store, "operator", now=later)
    assert apply(store, kwargs, handle=newer, clock=lambda: later)["next_key"] is None


def test_expiry_during_remote_inspection_prevents_first_put(tmp_path, monkeypatch):
    _, store, kwargs = fixture(tmp_path, monkeypatch)
    publisher.prepare_upload(store, **kwargs)
    current = [AT]
    download = store.download_file

    def advance_clock(*args, **options):
        download(*args, **options)
        current[0] = AT + LEASE_TTL

    monkeypatch.setattr(store, "download_file", advance_clock)
    with pytest.raises(LeaseError, match="expired"):
        apply(store, kwargs, clock=lambda: current[0])
    assert not store.requests


def test_get_race_is_not_accepted_as_a_stable_original(tmp_path, monkeypatch):
    _, store, kwargs = fixture(tmp_path, monkeypatch)
    publisher.prepare_upload(store, **kwargs)
    download = store.download_file

    def racing_download(key, path, **options):
        download(key, path, **options)
        if key == "runs.sqlite":
            store.store_object(key, b"concurrent change during GET")

    monkeypatch.setattr(store, "download_file", racing_download)
    with pytest.raises(ValueError, match="changed during readback"):
        apply(store, kwargs)
    assert not store.requests


@pytest.mark.parametrize("mode", ["local_upload", "backup", "unrelated", "unknown", "cas", "nonprefix"])
def test_tamper_race_and_unknown_remote_state_stop_without_rebasing(tmp_path, monkeypatch, mode):
    _, store, kwargs = fixture(tmp_path, monkeypatch)
    publisher.prepare_upload(store, **kwargs)
    directory = kwargs["directory"]
    original_plan = (directory / "plan.json").read_bytes()
    if mode == "local_upload":
        (directory / "runs.sqlite").write_bytes(b"tampered snapshot")
    elif mode == "backup":
        (directory / "backup" / "baibai.sqlite").write_bytes(b"tampered backup")
    elif mode == "unrelated":
        store.store_object("market.sqlite", b"other source fact")
    elif mode == "unknown":
        store.store_object("baibai.sqlite", b"new human report")
    elif mode == "nonprefix":
        store.store_object("baibai.sqlite", (directory / "baibai.sqlite").read_bytes())
    else:
        store.fault_key, store.fault = publisher.MACHINE, "conflict"
        with pytest.raises(LakeCASConflict, match="synthetic"):
            apply(store, kwargs)
    requests = len(store.requests)
    with pytest.raises(ValueError, match="changed|exact original|known publication|evidence"):
        apply(store, kwargs)
    assert len(store.requests) == requests
    assert (directory / "plan.json").read_bytes() == original_plan
