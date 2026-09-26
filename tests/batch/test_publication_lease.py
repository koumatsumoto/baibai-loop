"""The lease must survive races and ambiguous conditional R2 writes."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from baibai_batch.storage import publication_lease as lease
from baibai_batch.storage.lake_publish import LakeCASConflict, LakePublishError, RemoteObject

NOW = datetime(2026, 9, 26, 3, tzinfo=UTC)


class MemoryStore:
    def __init__(self) -> None:
        self.body: bytes | None = None
        self.etag = ""
        self.writes: list[tuple[str | None, bool]] = []
        self.fault: str | None = None
        self.other: bytes | None = None
        self.serial = 0

    def head(self, key: str) -> RemoteObject | None:
        assert key == lease.PUBLICATION_LEASE_KEY
        if self.body is None:
            return None
        return RemoteObject(self.etag, len(self.body), {}, "application/json")

    def get_bytes(self, key: str) -> bytes:
        assert key == lease.PUBLICATION_LEASE_KEY
        assert self.body is not None
        return self.body

    def put_file(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        content_md5: str,
        content_type: str,
        if_match: str | None = None,
        if_none_match: bool = False,
    ) -> RemoteObject:
        assert key == lease.PUBLICATION_LEASE_KEY
        assert sha256
        assert content_md5
        assert content_type == "application/json"
        self.writes.append((if_match, if_none_match))
        if self.fault == "other":
            self.fault = None
            self._set(self.other or b"")
            raise LakeCASConflict("racing writer")
        if (if_none_match and self.body is not None) or (
            if_match is not None and if_match != self.etag
        ):
            raise LakeCASConflict("stale")
        if self.fault == "before":
            self.fault = None
            raise LakePublishError("timeout")
        self._set(path.read_bytes())
        if self.fault == "after":
            self.fault = None
            raise LakePublishError("response lost")
        head = self.head(key)
        assert head is not None
        return head

    def _set(self, body: bytes) -> None:
        self.serial += 1
        self.body = body
        self.etag = str(self.serial)


@pytest.fixture(autouse=True)
def local_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def test_absent_acquire_busy_release_and_reacquire() -> None:
    store = MemoryStore()
    handle, expired = lease.acquire(store, "daily", now=NOW)
    assert not expired
    assert store.writes == [(None, True)]
    assert set(json.loads(store.body or b"{}")) == lease._WIRE_KEYS
    assert handle.owner.startswith("local:")
    with pytest.raises(lease.LeaseBusy):
        lease.acquire(store, "operator", now=NOW)
    assert len(store.writes) == 1
    lease.release(store, handle)
    assert store.writes[-1] == (handle.etag, False)
    assert lease._decode(store.body or b"").state == "released"
    newer, _ = lease.acquire(store, "operator", now=NOW)
    assert newer.etag != handle.etag
    assert store.writes[-1] == ("2", False)
    with pytest.raises(lease.LeaseError):
        lease.release(store, handle)
    assert lease._decode(store.body or b"").owner == newer.owner


def test_expired_takeover_and_race() -> None:
    store = MemoryStore()
    old, _ = lease.acquire(store, "daily", now=NOW)
    new, expired = lease.acquire(store, "tradingview", now=NOW + timedelta(minutes=120))
    assert expired
    assert store.writes[-1] == (old.etag, False)
    with pytest.raises(lease.LeaseBusy):
        lease.acquire(store, "operator", now=NOW + timedelta(minutes=120))
    with pytest.raises(lease.LeaseError):
        lease.release(store, old)
    assert lease._decode(store.body or b"").owner == new.owner


@pytest.mark.parametrize("fault", ["after", "before"])
def test_ambiguous_acquire_is_reconciled_once(fault: str) -> None:
    store = MemoryStore()
    store.fault = fault
    if fault == "after":
        handle, _ = lease.acquire(store, "daily", now=NOW)
        assert handle.etag == "1"
    else:
        with pytest.raises(lease.LeaseError, match="outcome unknown"):
            lease.acquire(store, "daily", now=NOW)
    assert len(store.writes) == 1


@pytest.mark.parametrize("state", ["held", "released", "expired"])
def test_competing_generation_is_busy_without_retry(state: str) -> None:
    store = MemoryStore()
    other = lease.PublicationLease(
        "released" if state == "released" else "held",
        "local:" + "a" * 32,
        "operator",
        NOW - lease.LEASE_TTL if state == "expired" else NOW,
        NOW if state == "expired" else NOW + lease.LEASE_TTL,
    )
    store.other = lease._encode(other)
    store.fault = "other"
    with pytest.raises(lease.LeaseBusy) as exc:
        lease.acquire(store, "daily", now=NOW)
    assert exc.value.lease == other
    assert len(store.writes) == 1


def test_release_ambiguous_commit_and_stale_owner() -> None:
    store = MemoryStore()
    handle, _ = lease.acquire(store, "daily", now=NOW)
    store.fault = "after"
    lease.release(store, handle)
    assert lease._decode(store.body or b"").state == "released"


@pytest.mark.parametrize(
    "change",
    [
        lambda value: {**value, "state": "unknown"},
        lambda value: {**value, "extra": 1},
        lambda value: {**value, "expires_at_utc": NOW.isoformat()},
        lambda value: {**value, "owner": "local:bad\nowner"},
        lambda value: {**value, "acquired_at_utc": "not-a-time"},
    ],
)
def test_malformed_lease_fails_closed(change: object) -> None:
    store = MemoryStore()
    valid = lease.PublicationLease("held", "local:" + "a" * 32, "daily", NOW, NOW + lease.LEASE_TTL)
    raw = json.loads(lease._encode(valid))
    store._set(json.dumps(change(raw)).encode())  # type: ignore[operator]
    with pytest.raises(lease.LeaseError):
        lease.acquire(store, "daily", now=NOW + lease.LEASE_TTL)
    assert store.writes == []


def test_handle_lifecycle_and_existing_file_fails_before_r2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = MemoryStore()
    monkeypatch.setattr(lease, "Boto3R2Store", lambda **_: store)
    path = tmp_path / "lease.json"
    path.write_text("previous")
    assert lease.main(["acquire", "--purpose", "daily", "--handle", str(path)]) == 1
    assert store.body is None
    path.unlink()
    assert lease.main(["acquire", "--purpose", "daily", "--handle", str(path)]) == 0
    assert set(json.loads(path.read_text())) == lease._HANDLE_KEYS
    assert lease.main(["release", "--handle", str(path)]) == 0
    assert not path.exists()


def test_missing_etag_rejects_release(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = MemoryStore()
    monkeypatch.setattr(lease, "Boto3R2Store", lambda **_: store)
    path = tmp_path / "handle.json"
    path.write_text(
        json.dumps(
            {
                "owner": "local:" + "a" * 32,
                "purpose": "daily",
                "etag": "",
                "acquired_at_utc": NOW.isoformat(),
                "expires_at_utc": (NOW + lease.LEASE_TTL).isoformat(),
            }
        )
    )
    assert lease.main(["release", "--handle", str(path)]) == 1
    assert store.writes == []
    assert path.exists()


def test_failed_release_keeps_handle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = MemoryStore()
    monkeypatch.setattr(lease, "Boto3R2Store", lambda **_: store)
    path = tmp_path / "handle.json"
    assert lease.main(["acquire", "--purpose", "daily", "--handle", str(path)]) == 0
    store.fault = "before"
    assert lease.main(["release", "--handle", str(path)]) == 1
    assert path.exists()
    assert lease._decode(store.body or b"").state == "held"


def test_handle_save_failure_releases_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = MemoryStore()
    monkeypatch.setattr(lease, "Boto3R2Store", lambda **_: store)

    def fail_save(_path: Path, _handle: lease.PublicationLeaseHandle) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(lease, "_save_handle", fail_save)
    assert lease.main(["acquire", "--purpose", "daily", "--handle", str(tmp_path / "x")]) == 1
    assert lease._decode(store.body or b"").state == "released"


def test_cli_uses_module_path_and_existing_bucket_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = MemoryStore()
    seen: dict[str, object] = {}

    def load(path: Path) -> None:
        seen["env_path"] = path

    def make_store(*, bucket: str) -> MemoryStore:
        seen["bucket"] = bucket
        return store

    monkeypatch.setattr(lease, "load_project_env", load)
    monkeypatch.setattr(lease, "Boto3R2Store", make_store)
    monkeypatch.setenv("R2_STORES_BUCKET", "test-stores")
    path = tmp_path / "handle.json"
    assert lease.main(["acquire", "--purpose", "operator", "--handle", str(path)]) == 0
    assert seen == {"env_path": Path(lease.__file__), "bucket": "test-stores"}


def test_owner_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    store = MemoryStore()
    with pytest.raises(lease.LeaseError):
        lease.acquire(store, "daily", now=NOW)
    assert store.body is None
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    handle, _ = lease.acquire(MemoryStore(), "daily", now=NOW)
    assert handle.owner == "github:123:2"
