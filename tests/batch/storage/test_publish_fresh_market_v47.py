"""The attended cutover is exercised entirely against a fake object store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from tests.helpers.l1_release import market_store
from tests.helpers.lake_policy import narrow_release_policy
from tests.helpers.r2_store import MemoryR2Store
from tools.cutover.publish_fresh_market_v47 import (
    MARKET_KEY,
    NEW_POINTER_KEY,
    OLD_POINTER_KEY,
    CutoverError,
    SourcePin,
    publish_fresh_market,
    recover_committed_origin,
)

from baibai_batch.storage.lake_publish import LakeCASConflict, LakePublishError
from baibai_batch.storage.publication_lease import (
    LeaseError,
    PublicationLeaseHandle,
    acquire,
    release,
)
from baibai_engine.storage.observations.lake_origin import read_lake_store_origin


def _wire(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class CutoverFake(MemoryR2Store):
    def __init__(self) -> None:
        super().__init__()
        self.before_release_put: Callable[[], None] | None = None
        self.pointer_behavior: str | None = None
        self.triggered = False
        self.header_reads = 0

    def sqlite_user_version(self, key: str, *, etag: str) -> int:
        assert key == MARKET_KEY
        assert self.head(key).etag == etag  # type: ignore[union-attr]
        self.header_reads += 1
        return 39

    def put_file(self, key: str, path: Path, **kwargs):  # type: ignore[no-untyped-def]
        if key.startswith("lake/manifests/releases/market/") and not self.triggered:
            self.triggered = True
            if self.before_release_put is not None:
                self.before_release_put()
        if key == NEW_POINTER_KEY:
            if self.pointer_behavior == "fail":
                raise LakePublishError("simulated pointer PUT failure")
            if self.pointer_behavior == "uncertain_success":
                super().put_file(key, path, **kwargs)
                raise LakePublishError("simulated timeout after committed PUT")
            if self.pointer_behavior == "race":
                other = "20261019T000000Z-other"
                self.store_object(
                    NEW_POINTER_KEY,
                    _wire(
                        {
                            "pointer_version": 1,
                            "release_id": other,
                            "manifest_key": f"lake/manifests/releases/market/{other}.json",
                            "manifest_sha256": "b" * 64,
                        }
                    ),
                )
        return super().put_file(key, path, **kwargs)


def _source(store: CutoverFake, *, daily_floor: str = "2026-01-05") -> SourcePin:
    release_id = "20261018T163124Z-old"
    contracts = {"jquants.daily_bars": 2, "jquants.short_sale_reports": 2}
    entries: dict[str, dict[str, object]] = {}
    for name, floor in (
        ("jquants.daily_bars", daily_floor),
        ("jquants.short_sale_reports", "2026-01-06"),
    ):
        build_id = f"old-{name.replace('.', '-')}"
        shared = {
            "build_id": build_id,
            "contract_version": 2,
            "totals": {"rows": 1},
            "data_as_of": "2026-02-28",
            "coverage_status": "complete",
        }
        dataset = {**shared, "manifest_version": 1, "dataset": name, "coverage_start": floor}
        digest = _sha(_wire(dataset))
        key = f"lake/manifests/datasets/{name}/{build_id}.json"
        store.store_object(key, _wire(dataset))
        entries[name] = {**shared, "manifest_sha256": digest}
    release_key = f"lake/manifests/releases/l1/{release_id}.json"
    release_body = _wire(
        {
            "manifest_version": 1,
            "release_id": release_id,
            "profile": "production",
            "datasets": entries,
        }
    )
    store.store_object(release_key, release_body)
    pointer_body = _wire(
        {
            "pointer_version": 1,
            "release_id": release_id,
            "manifest_key": release_key,
            "manifest_sha256": _sha(release_body),
        }
    )
    store.store_object(OLD_POINTER_KEY, pointer_body)
    store.store_object(
        MARKET_KEY, b"old market header fake", content_type="application/octet-stream"
    )
    for key in ("application.sqlite", "runs.sqlite"):
        store.store_object(key, key.encode(), content_type="application/octet-stream")
    return SourcePin(
        pointer_etag=store.head(OLD_POINTER_KEY).etag,  # type: ignore[union-attr]
        pointer_sha256=_sha(pointer_body),
        release_id=release_id,
        manifest_sha256=_sha(release_body),
        market_etag=store.head(MARKET_KEY).etag,  # type: ignore[union-attr]
        market_bytes=len(b"old market header fake"),
        market_schema=39,
        contracts=contracts,
    )


@pytest.fixture
def setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, CutoverFake, PublicationLeaseHandle]:
    narrow_release_policy(monkeypatch, carries_history=True)
    candidate = market_store(tmp_path / "fresh.sqlite")
    mirror = tmp_path / "mirror"
    store = CutoverFake()
    lease, _ = acquire(store, "operator")
    return candidate, mirror, store, lease


def _publish(
    setup: tuple[Path, Path, CutoverFake, PublicationLeaseHandle], pin: SourcePin
) -> dict[str, object]:
    candidate, mirror, store, lease = setup
    return publish_fresh_market(
        candidate=candidate,
        mirror=mirror,
        store=store,
        lease=lease,
        pin=pin,
        release_id="20261019T000000Z-fresh",
        producer_git_commit="a" * 40,
        recovery_record=mirror.parent / "recovery.json",
    )


def _assert_old_unchanged(store: CutoverFake) -> None:
    assert store.values["application.sqlite"].body == b"application.sqlite"
    assert store.values["runs.sqlite"].body == b"runs.sqlite"
    assert store.values[MARKET_KEY].body == b"old market header fake"


def test_success_publishes_then_advances_origin(setup) -> None:  # type: ignore[no-untyped-def]
    candidate, _, store, _ = setup
    pin = _source(store)
    result = _publish(setup, pin)
    assert result["old_dataset_count"] == 2
    assert result["market_machine_store_pushed"] is False
    assert NEW_POINTER_KEY in store.values
    origin = read_lake_store_origin(candidate)
    assert origin is not None
    assert origin.release_id == result["release_id"]
    _assert_old_unchanged(store)
    assert MARKET_KEY not in store.get_keys
    assert store.header_reads >= 2


def test_uncertain_successful_pointer_put_is_read_back(setup) -> None:  # type: ignore[no-untyped-def]
    candidate, _, store, _ = setup
    pin = _source(store)
    store.pointer_behavior = "uncertain_success"
    result = _publish(setup, pin)
    origin = read_lake_store_origin(candidate)
    assert origin is not None
    assert result["release_id"] == origin.release_id
    _assert_old_unchanged(store)


def test_failed_pointer_put_keeps_origin_unset(setup) -> None:  # type: ignore[no-untyped-def]
    candidate, _, store, _ = setup
    pin = _source(store)
    store.pointer_behavior = "fail"
    with pytest.raises(LakePublishError):
        _publish(setup, pin)
    assert NEW_POINTER_KEY not in store.values
    assert read_lake_store_origin(candidate) is None
    _assert_old_unchanged(store)


def test_lease_loss_before_pointer_keeps_origin_unset(setup) -> None:  # type: ignore[no-untyped-def]
    candidate, _, store, lease = setup
    pin = _source(store)
    store.before_release_put = lambda: release(store, lease)
    with pytest.raises(LeaseError):
        _publish(setup, pin)
    assert NEW_POINTER_KEY not in store.values
    assert read_lake_store_origin(candidate) is None


def test_pointer_race_keeps_origin_unset(setup) -> None:  # type: ignore[no-untyped-def]
    candidate, _, store, _ = setup
    pin = _source(store)
    store.pointer_behavior = "race"
    with pytest.raises(LakeCASConflict):
        _publish(setup, pin)
    assert read_lake_store_origin(candidate) is None


def test_prior_history_omission_stops_before_any_upload(setup) -> None:  # type: ignore[no-untyped-def]
    candidate, _, store, _ = setup
    pin = _source(store, daily_floor="2025-01-05")
    with pytest.raises(CutoverError, match="omits prior history"):
        _publish(setup, pin)
    assert NEW_POINTER_KEY not in store.values
    assert read_lake_store_origin(candidate) is None
    assert not any(key.startswith("lake/l1/") for key in store.put_keys)


def test_missing_prior_dataset_stops_before_export(setup) -> None:  # type: ignore[no-untyped-def]
    candidate, _, store, _ = setup
    pin = _source(store)
    with sqlite3.connect(candidate) as connection:
        connection.execute("DELETE FROM jquants_short_sale_reports")
    with pytest.raises(CutoverError, match=r"jquants\.short_sale_reports.*candidate=None"):
        _publish(setup, pin)
    assert NEW_POINTER_KEY not in store.values
    assert read_lake_store_origin(candidate) is None
    assert not any(key.startswith("lake/l1/") for key in store.put_keys)


def test_input_change_before_pointer_stops(setup) -> None:  # type: ignore[no-untyped-def]
    candidate, _, store, _ = setup
    pin = _source(store)

    def change() -> None:
        with sqlite3.connect(candidate) as connection:
            connection.execute(
                "UPDATE jquants_daily_bars SET close = close + 1 WHERE ticker = '1301'"
            )

    store.before_release_put = change
    with pytest.raises(CutoverError, match="candidate SQLite changed"):
        _publish(setup, pin)
    assert NEW_POINTER_KEY not in store.values
    assert read_lake_store_origin(candidate) is None


def test_source_generation_change_stops_before_export(setup) -> None:  # type: ignore[no-untyped-def]
    candidate, _, store, _ = setup
    pin = _source(store)
    store.store_object(OLD_POINTER_KEY, b"changed")
    with pytest.raises(CutoverError, match="source generation changed"):
        _publish(setup, pin)
    assert NEW_POINTER_KEY not in store.values
    assert read_lake_store_origin(candidate) is None


def test_recovery_requires_exact_committed_release_and_unchanged_candidate(
    setup, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    candidate, mirror, store, lease = setup
    pin = _source(store)
    from tools.cutover import publish_fresh_market_v47 as tool

    original = tool.advance_lake_store_origin
    monkeypatch.setattr(
        tool,
        "advance_lake_store_origin",
        lambda *args, **kwargs: (_ for _ in ()).throw(CutoverError("simulated crash")),
    )
    with pytest.raises(CutoverError, match="simulated crash"):
        _publish(setup, pin)
    assert read_lake_store_origin(candidate) is None
    assert NEW_POINTER_KEY in store.values
    record = mirror.parent / "recovery.json"
    assert record.exists()
    monkeypatch.setattr(tool, "advance_lake_store_origin", original)
    outcome = recover_committed_origin(
        candidate=candidate, mirror=mirror, record_path=record, store=store, lease=lease, pin=pin
    )
    assert outcome["origin"] == "advanced_after_readback"
    with pytest.raises(CutoverError, match="origin is already set"):
        recover_committed_origin(
            candidate=candidate,
            mirror=mirror,
            record_path=record,
            store=store,
            lease=lease,
            pin=pin,
        )
    _assert_old_unchanged(store)


def test_recovery_refuses_other_new_pointer(setup, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    candidate, mirror, store, lease = setup
    pin = _source(store)
    from tools.cutover import publish_fresh_market_v47 as tool

    monkeypatch.setattr(
        tool,
        "advance_lake_store_origin",
        lambda *args, **kwargs: (_ for _ in ()).throw(CutoverError("simulated crash")),
    )
    with pytest.raises(CutoverError, match="simulated crash"):
        _publish(setup, pin)
    body = json.loads(store.values[NEW_POINTER_KEY].body)
    body["release_id"] = "20261019T000000Z-other"
    body["manifest_key"] = "lake/manifests/releases/market/20261019T000000Z-other.json"
    store.store_object(NEW_POINTER_KEY, _wire(body))
    with pytest.raises(CutoverError, match="exact committed recovery release"):
        recover_committed_origin(
            candidate=candidate,
            mirror=mirror,
            record_path=mirror.parent / "recovery.json",
            store=store,
            lease=lease,
            pin=pin,
        )
    assert read_lake_store_origin(candidate) is None
