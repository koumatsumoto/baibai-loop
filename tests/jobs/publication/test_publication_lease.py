"""The lease must survive races and ambiguous conditional R2 writes."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import baibai_jobs.publication.lease as lease
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    R2Error,
    RemoteObject,
)

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
        body = self.body
        if self.fault == "read_change":
            self.fault = None
            self._set(body)
        return body

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
            raise R2Error("timeout")
        self._set(path.read_bytes())
        if self.fault == "after":
            self.fault = None
            raise R2Error("response lost")
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
    assert set(json.loads(store.body or b"{}")) == lease._WIRE_KEYS | {"protocol_version"}
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


@pytest.mark.parametrize("state", ["held", "released"])
@pytest.mark.parametrize(
    ("purpose", "minutes"), [("operator", 120), ("daily", 120), ("daily", 180), ("daily", 220)]
)
def test_legacy_handle_finishes_before_normal_acquire_establishes_v2(
    state, purpose, minutes, tmp_path
):
    store = MemoryStore()
    old = lease.PublicationLease(
        state,
        "local:" + "a" * 32,
        purpose,
        NOW,
        NOW + timedelta(minutes=minutes),
        protocol_version=1,
    )
    store._set(lease._encode(old))
    assert set(json.loads(store.body or b"{}")) == lease._WIRE_KEYS
    handle = lease.PublicationLeaseHandle(
        old.owner,
        old.purpose,
        store.etag,
        old.acquired_at_utc,
        old.expires_at_utc,
        protocol_version=1,
    )
    path = tmp_path / "legacy-handle.json"
    lease._save_handle(path, handle)
    assert set(json.loads(path.read_bytes())) == lease._HANDLE_KEYS
    assert lease._load_handle(path) == handle
    if state == "held":
        lease.verify_held(store, handle, now=NOW)
        with pytest.raises(lease.LeaseBusy):
            lease.acquire(store, "operator", now=NOW)
        assert store.writes == []
        lease.release(store, handle)
        assert lease._decode(store.body or b"").protocol_version == 1
    old_etag = store.etag
    upgraded, expired = lease.acquire(store, "operator", now=NOW)
    assert not expired
    assert store.writes[-1] == (old_etag, False)
    assert upgraded.protocol_version == 2
    assert json.loads(store.body or b"{}")["protocol_version"] == 2
    lease.release(store, upgraded)
    assert lease._decode(store.body or b"").protocol_version == 2
    assert set(json.loads(store.body or b"{}")) != lease._WIRE_KEYS
    before = store.body, store.etag, list(store.writes)
    with pytest.raises(lease.LeaseError, match="downgrade"):
        lease.release(store, handle)
    assert (store.body, store.etag, store.writes) == before


@pytest.mark.parametrize("protocol", [None, True, 1, "2", 3])
def test_explicit_unknown_protocol_is_not_a_legacy_record_or_handle(protocol, tmp_path):
    store = MemoryStore()
    handle, _ = lease.acquire(store, "operator", now=NOW)
    raw = json.loads(store.body or b"{}")
    raw["protocol_version"] = protocol
    store._set(json.dumps(raw).encode())
    store.writes.clear()
    with pytest.raises(lease.LeaseError, match="protocol"):
        lease.acquire(store, "daily", now=NOW + lease.LEASE_TTL)
    assert store.writes == []
    raw_handle = json.loads(lease._handle_payload(handle))
    raw_handle["protocol_version"] = protocol
    path = tmp_path / "invalid-handle.json"
    path.write_text(json.dumps(raw_handle))
    with pytest.raises(lease.LeaseError, match="protocol"):
        lease._load_handle(path)


def test_v1_to_v2_upgrade_loses_to_an_actual_legacy_acquire_without_retry():
    store = MemoryStore()
    old = lease.PublicationLease(
        "released",
        "local:" + "a" * 32,
        "operator",
        NOW,
        NOW + lease.LEASE_TTL,
        protocol_version=1,
    )
    store._set(lease._encode(old))
    expected_etag = store.etag
    other = replace(old, state="held", owner="local:" + "b" * 32)
    store.other = lease._encode(other)
    store.fault = "other"
    with pytest.raises(lease.LeaseBusy) as error:
        lease.acquire(store, "daily", now=NOW)
    assert error.value.lease == other
    assert store.body == lease._encode(other)
    assert store.writes == [(expected_etag, False)]


def test_truncated_v2_handle_cannot_release_as_v1_even_with_the_current_etag(tmp_path):
    store = MemoryStore()
    handle, _ = lease.acquire(store, "operator", now=NOW)
    path = tmp_path / "truncated-handle.json"
    raw = json.loads(lease._handle_payload(handle))
    del raw["protocol_version"]
    path.write_text(json.dumps(raw))
    truncated = lease._load_handle(path)
    before = store.body, store.etag, list(store.writes)
    with pytest.raises(lease.LeaseError, match="expired or replaced"):
        lease.verify_held(store, truncated, now=NOW)
    with pytest.raises(lease.LeaseError, match="downgrade"):
        lease.release(store, truncated)
    assert (store.body, store.etag, store.writes) == before


def test_legacy_release_cannot_overwrite_an_upgrade_after_its_preflight_read():
    store = MemoryStore()
    old = lease.PublicationLease(
        "held",
        "local:" + "a" * 32,
        "operator",
        NOW,
        NOW + lease.LEASE_TTL,
        protocol_version=1,
    )
    store._set(lease._encode(old))
    handle = lease.PublicationLeaseHandle(
        old.owner,
        old.purpose,
        store.etag,
        old.acquired_at_utc,
        old.expires_at_utc,
        protocol_version=1,
    )
    newer = replace(old, owner="local:" + "b" * 32, protocol_version=2)
    store.other = lease._encode(newer)
    store.fault = "other"
    with pytest.raises(lease.LeaseError, match="conflicted with another owner"):
        lease.release(store, handle)
    assert store.writes == [(handle.etag, False)]
    assert store.body == lease._encode(newer)


@pytest.mark.parametrize("fault", ["before", "after"])
def test_legacy_upgrade_ambiguous_write_is_observed_without_retry(fault):
    store = MemoryStore()
    old = lease.PublicationLease(
        "released",
        "local:" + "a" * 32,
        "operator",
        NOW,
        NOW + lease.LEASE_TTL,
        protocol_version=1,
    )
    old_payload = lease._encode(old)
    store._set(old_payload)
    old_etag = store.etag
    store.fault = fault
    if fault == "before":
        with pytest.raises(lease.LeaseError, match="write did not complete"):
            lease.acquire(store, "daily", now=NOW)
        assert store.body == old_payload
    else:
        handle, _ = lease.acquire(store, "daily", now=NOW)
        assert handle.protocol_version == 2
        lease.verify_held(store, handle, now=NOW)
    assert store.writes == [(old_etag, False)]


def test_expired_takeover_and_race() -> None:
    store = MemoryStore()
    old, _ = lease.acquire(store, "daily", now=NOW)
    new, expired = lease.acquire(store, "tradingview", now=NOW + lease.DAILY_LEASE_TTL)
    assert expired
    assert store.writes[-1] == (old.etag, False)
    with pytest.raises(lease.LeaseBusy):
        lease.acquire(store, "operator", now=NOW + lease.DAILY_LEASE_TTL)
    with pytest.raises(lease.LeaseError):
        lease.release(store, old)
    assert lease._decode(store.body or b"").owner == new.owner


@pytest.mark.parametrize("purpose", ["daily", "tradingview", "materialize", "operator"])
def test_only_new_daily_leases_receive_the_longer_budget(purpose: str) -> None:
    store = MemoryStore()
    handle, _ = lease.acquire(store, purpose, now=NOW)
    expected = timedelta(minutes=220 if purpose == "daily" else 120)
    assert handle.expires_at_utc - handle.acquired_at_utc == expected
    lease.verify_held(store, handle, now=handle.expires_at_utc - timedelta(seconds=1))
    with pytest.raises(lease.LeaseError, match="expired or replaced"):
        lease.verify_held(store, handle, now=handle.expires_at_utc)


@pytest.mark.parametrize("elapsed_minutes", [121, 181, 219])
def test_new_daily_still_excludes_a_competitor_after_the_old_expiry(elapsed_minutes: int) -> None:
    store = MemoryStore()
    handle, _ = lease.acquire(store, "daily", now=NOW)
    before = store.body, store.etag, list(store.writes)
    with pytest.raises(lease.LeaseBusy):
        lease.acquire(store, "operator", now=NOW + timedelta(minutes=elapsed_minutes))
    lease.verify_held(store, handle, now=NOW + timedelta(minutes=elapsed_minutes))
    assert (store.body, store.etag, store.writes) == before


@pytest.mark.parametrize("state", ["held", "released"])
@pytest.mark.parametrize("minutes", [120, 180])
def test_old_daily_lease_and_handle_keep_their_exact_expiry(
    state: str, minutes: int, tmp_path: Path
) -> None:
    store = MemoryStore()
    old = lease.PublicationLease(
        state, "local:" + "a" * 32, "daily", NOW, NOW + timedelta(minutes=minutes)
    )
    store._set(lease._encode(old))
    handle = lease.PublicationLeaseHandle(
        old.owner, old.purpose, store.etag, old.acquired_at_utc, old.expires_at_utc
    )
    path = tmp_path / "old-handle.json"
    lease._save_handle(path, handle)
    assert lease._load_handle(path) == handle
    assert lease._decode(store.body or b"") == old
    before_expiry = old.expires_at_utc - timedelta(seconds=1)
    if state == "held":
        lease.verify_held(store, handle, now=before_expiry)
        with pytest.raises(lease.LeaseBusy):
            lease.acquire(store, "daily", now=before_expiry)
        assert store.writes == []
        lease.release(store, handle)
        assert lease._decode(store.body or b"").expires_at_utc == old.expires_at_utc
    newer, expired = lease.acquire(store, "daily", now=before_expiry)
    assert not expired
    assert newer.expires_at_utc == before_expiry + timedelta(minutes=220)


@pytest.mark.parametrize("minutes", [120, 180])
def test_old_daily_lease_can_be_taken_over_at_its_original_expiry(minutes: int) -> None:
    store = MemoryStore()
    old = lease.PublicationLease(
        "held", "local:" + "a" * 32, "daily", NOW, NOW + timedelta(minutes=minutes)
    )
    store._set(lease._encode(old))
    old_etag = store.etag
    old_handle = lease.PublicationLeaseHandle(
        old.owner, old.purpose, old_etag, old.acquired_at_utc, old.expires_at_utc
    )
    with pytest.raises(lease.LeaseError, match="expired or replaced"):
        lease.verify_held(store, old_handle, now=old.expires_at_utc)
    newer, expired = lease.acquire(store, "daily", now=old.expires_at_utc)
    assert expired
    assert store.writes == [(old_etag, False)]
    assert newer.expires_at_utc == old.expires_at_utc + timedelta(minutes=220)
    with pytest.raises(lease.LeaseError, match="another owner"):
        lease.release(store, old_handle)
    assert lease._decode(store.body or b"").owner == newer.owner


@pytest.mark.parametrize(
    ("purpose", "minutes"),
    [
        *(("daily", minutes) for minutes in (119, 121, 179, 181, 219, 221)),
        *(
            (purpose, minutes)
            for purpose in ("operator", "tradingview", "materialize")
            for minutes in (180, 220)
        ),
    ],
)
@pytest.mark.parametrize("state", ["held", "released"])
def test_longer_budget_is_not_an_arbitrary_ttl(
    purpose: str, minutes: int, state: str, tmp_path: Path
) -> None:
    store = MemoryStore()
    invalid = lease.PublicationLease(
        state, "local:" + "a" * 32, purpose, NOW, NOW + timedelta(minutes=minutes)
    )
    store._set(lease._encode(invalid))
    with pytest.raises(lease.LeaseError, match="invalid publication lease TTL"):
        lease._decode(store.body or b"")
    path = tmp_path / "invalid-handle.json"
    lease._save_handle(
        path,
        lease.PublicationLeaseHandle(
            invalid.owner,
            invalid.purpose,
            store.etag,
            invalid.acquired_at_utc,
            invalid.expires_at_utc,
        ),
    )
    with pytest.raises(lease.LeaseError, match="invalid publication lease TTL"):
        lease._load_handle(path)
    with pytest.raises(lease.LeaseError, match="invalid publication lease TTL"):
        lease.acquire(store, "daily", now=NOW)
    assert store.writes == []


def test_generation_change_during_get_refuses_acquire_without_put() -> None:
    store = MemoryStore()
    previous = lease.PublicationLease(
        "released", "local:" + "a" * 32, "operator", NOW, NOW + lease.LEASE_TTL
    )
    store._set(lease._encode(previous))
    store.fault = "read_change"
    with pytest.raises(lease.LeaseError, match="changed during read"):
        lease.acquire(store, "daily", now=NOW)
    assert store.writes == []


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


def test_existing_released_lease_write_failure_is_not_busy() -> None:
    store = MemoryStore()
    previous = lease.PublicationLease(
        "released", "local:" + "a" * 32, "operator", NOW, NOW + lease.LEASE_TTL
    )
    store._set(lease._encode(previous))
    original_etag = store.etag
    store.fault = "before"
    with pytest.raises(lease.LeaseError, match="write did not complete"):
        lease.acquire(store, "daily", now=NOW)
    assert store.etag == original_etag
    assert store.writes == [(original_etag, False)]


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
    assert set(json.loads(path.read_text())) == lease._HANDLE_KEYS | {"protocol_version"}
    assert lease.main(["release", "--handle", str(path)]) == 0
    assert not path.exists()


@pytest.mark.parametrize("state", ["held", "expired", "released", "replaced", "missing"])
def test_verify_cli_checks_current_authority_without_writes_or_handle_changes(
    monkeypatch, tmp_path, capsys, state
):
    store = MemoryStore()
    now = datetime.now(UTC)
    handle, _ = lease.acquire(
        store, "operator", now=now - lease.LEASE_TTL if state == "expired" else now
    )
    path = tmp_path / "lease.json"
    lease._save_handle(path, handle)
    before = path.read_bytes()
    if state in {"released", "replaced"}:
        lease.release(store, handle)
    if state == "replaced":
        lease.acquire(store, "operator", now=now)
    if state == "missing":
        store.body = None
    store.writes.clear()
    monkeypatch.setattr(lease, "_load_project_env", lambda _: None)
    monkeypatch.setattr(lease, "Boto3R2Store", lambda **_: store)

    assert lease.main(["verify", "--handle", str(path)]) == int(state != "held")
    assert store.writes == []
    assert path.read_bytes() == before
    output = capsys.readouterr()
    assert ("verified purpose=operator" in output.out) == (state == "held")


@pytest.mark.parametrize("body", [None, "{", "{}"])
def test_verify_cli_rejects_missing_or_malformed_handle_before_r2(monkeypatch, tmp_path, body):
    path = tmp_path / "lease.json"
    if body is not None:
        path.write_text(body)

    def unexpected_store(**_):
        pytest.fail("invalid local handle must fail before constructing an R2 client")

    monkeypatch.setattr(lease, "Boto3R2Store", unexpected_store)
    assert lease.main(["verify", "--handle", str(path)]) == 1


@pytest.mark.parametrize("command", ["acquire", "release", "verify"])
def test_documented_parser_is_pure_and_requires_the_existing_options(monkeypatch, command):
    def unexpected(*_args, **_kwargs):
        pytest.fail("building and parsing lease commands must not load credentials or R2")

    monkeypatch.setattr(lease, "_load_project_env", unexpected)
    monkeypatch.setattr(lease, "Boto3R2Store", unexpected)
    parser = lease.build_parser()
    with pytest.raises(SystemExit) as missing:
        parser.parse_args([command])
    assert missing.value.code == 2
    arguments = [command, "--handle", "handle.json"]
    if command == "acquire":
        with pytest.raises(SystemExit) as purpose:
            parser.parse_args(arguments)
        assert purpose.value.code == 2
        arguments += ["--purpose", "operator"]
    parsed = parser.parse_args(arguments)
    assert parsed.command == command
    assert parsed.handle == Path("handle.json")


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

    monkeypatch.setattr(lease, "_load_project_env", load)
    monkeypatch.setattr(lease, "Boto3R2Store", make_store)
    monkeypatch.setenv("R2_STORES_BUCKET", "test-stores")
    path = tmp_path / "handle.json"
    assert lease.main(["acquire", "--purpose", "operator", "--handle", str(path)]) == 0
    assert seen == {"env_path": Path(lease.__file__), "bucket": "test-stores"}


def test_local_env_loads_from_module_location_without_overriding_process_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = tmp_path / "jobs/src/baibai_jobs/publication/lease.py"
    module.parent.mkdir(parents=True)
    (tmp_path / ".env").write_text("R2_STORES_BUCKET=from-file\nR2_ACCOUNT_ID=from-file\n")
    monkeypatch.setenv("R2_ACCOUNT_ID", "from-process")
    # Register an absent key too: dotenv writes outside monkeypatch tracking.
    monkeypatch.setenv("R2_STORES_BUCKET", "")
    monkeypatch.delenv("R2_STORES_BUCKET")
    monkeypatch.chdir(module.parent)
    lease._load_project_env(module)
    assert lease.os.environ["R2_ACCOUNT_ID"] == "from-process"
    assert lease.os.environ["R2_STORES_BUCKET"] == "from-file"


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
