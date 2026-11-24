"""The current head switches a complete pair, or leaves the preceding pair readable."""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager, nullcontext
from datetime import date
from pathlib import Path

import pytest
from botocore.exceptions import ClientError
from tests.helpers.lake_release import lake as lake
from tests.helpers.lake_release import market_store
from tests.helpers.model_generation import publish_machine_generation
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.storage.objects.r2 import (
    Boto3R2Store,
    LakeCASConflict,
    LakePublishError,
    R2Error,
    R2OperationStopped,
    R2PinnedObjectMismatch,
)
from baibai_engine.storage.snapshots.duck import lake_session
from baibai_engine.storage.snapshots.keys import current_release_pointer_key, release_manifest_key
from baibai_engine.storage.snapshots.models import (
    ReleaseManifest,
    canonical_lake_model_bytes,
    load_lake_model_json,
)
from baibai_engine.storage.snapshots.objects import LakeObjectCache, LocalMirrorSource
from baibai_engine.storage.snapshots.reader import LakeReadError
from baibai_engine.storage.snapshots.release import LakeReleasePointer, create_release
from baibai_engine.storage.sqlite.source_cache.lake_origin import (
    LakeStoreOrigin,
    read_lake_store_origin,
    write_lake_store_origin,
)
from baibai_jobs.integration import models
from baibai_jobs.publication import machine_current as machine
from baibai_jobs.publication import machine_pull, snapshot_write_cli
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.lease import LeaseError, _save_handle, acquire, release, verify_held
from baibai_jobs.publication.machine_pull import pull_current_machine
from baibai_jobs.publication.policy import release_policy_for_profile
from baibai_jobs.publication.snapshot_release import read_pointer_snapshot, stage_release

HEAD = current_release_pointer_key()
pytestmark = pytest.mark.usefixtures("lake")


def _lake(name: str) -> LakeReleasePointer:
    return LakeReleasePointer(
        release_id=name,
        manifest_key=release_manifest_key(release_id=name),
        manifest_sha256=hashlib.sha256(name.encode()).hexdigest(),
    )


def _files(tmp_path: Path, pointer: LakeReleasePointer) -> tuple[Path, Path]:
    name, digest = pointer.release_id, pointer.manifest_sha256
    market = market_store(tmp_path / f"{name}.market.sqlite")
    with closing(sqlite3.connect(market)) as db, db:
        write_lake_store_origin(db, LakeStoreOrigin(name, digest))
    runs = tmp_path / f"{name}.runs.sqlite"
    publish_machine_generation(runs, release_id=name, release_sha256=digest)
    return market, runs


def _stage_lake(store, tmp_path, name):
    base = read_pointer_snapshot(store)
    mirror = tmp_path / "mirror"
    release_path, _ = create_release(
        dataset_manifest_paths=sorted((mirror / "lake/manifests/datasets").glob("*/*.json")),
        mirror_root=mirror,
        release_id=name,
        catalog=LAKE_DATASETS,
        policy=release_policy_for_profile("production"),
    )
    return stage_release(
        mirror_root=mirror, release_manifest_path=release_path, store=store, base=base
    )


def _stage_members(store, *, lake, market, runs):
    ready = machine.finish_machine_retirement(store, lake.base, while_held=lambda: None)
    return machine.stage_machine_members(store, lake=lake, base=ready, market=market, runs=runs)


def _publish_machine(store, *, lake, market, runs, while_held, base=None):
    ready = base or machine.finish_machine_retirement(store, lake.base, while_held=while_held)
    return machine.publish_staged_machine(
        store, lake=lake, market=market, runs=runs, while_held=while_held, base=ready
    )


def _candidate(store, tmp_path, name):
    lake = _stage_lake(store, tmp_path, name)
    market, runs = _files(tmp_path, lake.pointer)
    return _stage_members(store, lake=lake, market=market, runs=runs)


def _activate(store, candidate, before_switch=lambda: None):
    return machine.activate_machine(store, candidate, before_switch=before_switch)


@pytest.fixture
def incumbent(tmp_path):
    store = ReadStore()
    pointer = _candidate(store, tmp_path, "old")
    result = _activate(store, pointer)
    assert result.detached == ()
    return store, pointer.pointer, result.current


def test_lake_only_fixtures_keep_wire_but_are_not_a_machine_current(incumbent):
    store, _, _ = incumbent
    old = _lake("lake-only")
    assert b"machine" not in canonical_lake_model_bytes(old)
    store.store_object(HEAD, canonical_lake_model_bytes(old))
    with pytest.raises(machine.MachineMemberUnavailable, match="does not name a machine pair"):
        machine.read_machine_current(store)


def test_interrupted_second_upload_keeps_previous_head_and_both_old_members(
    incumbent,
    tmp_path,
    monkeypatch,
):
    store, pointer, base = incumbent
    real_put = store.put_file

    def fail_runs(key, *args, **kwargs):
        if key.endswith("/runs.sqlite"):
            raise R2Error("injected failure during second upload")
        return real_put(key, *args, **kwargs)

    monkeypatch.setattr(store, "put_file", fail_runs)
    with pytest.raises(machine.MachineStageInterrupted) as error:
        _candidate(store, tmp_path, "not-public")
    assert isinstance(error.value.__cause__, R2Error)
    assert machine.read_machine_current(store) == base
    assert pointer.machine is not None
    staged = [key for key in store.put_keys if key.endswith("/market.sqlite")]
    assert staged[0] == pointer.machine.market.key
    assert len(staged) == 2
    assert staged[1] != staged[0]
    assert error.value.detached == (staged[1],)
    assert error.value.unconfirmed == staged[1].removesuffix("market.sqlite") + "runs.sqlite"
    assert [k for k in store.values if k.endswith("/runs.sqlite")] == [pointer.machine.runs.key]
    assert store.put_keys.count(HEAD) == 1


def test_there_is_one_winner_and_only_its_predecessor_is_detached(incumbent, tmp_path):
    store, first, base = incumbent
    winner = _candidate(store, tmp_path, "winner")
    loser = _candidate(store, tmp_path, "stale")
    assert machine.read_machine_current(store) == base
    outcome = _activate(store, winner)
    assert first.machine is not None
    assert winner.pointer.machine is not None
    assert loser.pointer.machine is not None
    assert outcome.detached == (first.machine.market.key, first.machine.runs.key)
    assert not set(outcome.detached) & {
        winner.pointer.machine.market.key,
        winner.pointer.machine.runs.key,
    }
    with pytest.raises(LakeCASConflict):
        _activate(store, loser)
    assert machine.read_machine_current(store) == outcome.current
    assert set(store.values) >= {loser.pointer.machine.market.key, loser.pointer.machine.runs.key}


def test_an_unconfirmed_put_cannot_report_detachment_and_can_commit_late(
    incumbent,
    tmp_path,
    monkeypatch,
):
    store, first, base = incumbent
    new = _candidate(store, tmp_path, "late")
    real_put = store.put_file
    assert new.pointer.machine is not None
    pending = []

    def delayed(key, path, **options):
        assert key == HEAD
        assert options["if_match"] == base.precondition.etag
        pending.append((path.read_bytes(), options))
        raise R2Error("lost connection before commit")

    monkeypatch.setattr(store, "put_file", delayed)
    with pytest.raises(machine.MachineSwitchUnconfirmed):
        _activate(store, new)
    assert machine.read_machine_current(store) == base
    assert first.machine is not None
    assert set(store.values) >= {
        first.machine.market.key,
        first.machine.runs.key,
        new.pointer.machine.market.key,
        new.pointer.machine.runs.key,
    }

    # A negative observation could not cancel the admitted request.
    body, options = pending.pop()
    captured = tmp_path / "late-head.json"
    captured.write_bytes(body)
    real_put(HEAD, captured, **options)
    assert machine.read_machine_current(store).pointer == new.pointer


def test_a_lost_ack_can_be_reconciled_by_exact_current_bytes(incumbent, tmp_path, monkeypatch):
    store, first, _base = incumbent
    new = _candidate(store, tmp_path, "ack-lost")
    real_put = store.put_file

    def lost_ack(key, path, **options):
        real_put(key, path, **options)
        raise R2Error("lost connection after starting PUT")

    monkeypatch.setattr(store, "put_file", lost_ack)
    outcome = _activate(store, new)
    assert outcome.current.pointer == new.pointer
    assert first.machine is not None
    assert outcome.detached == (first.machine.market.key, first.machine.runs.key)


def test_require_live_pair_before_switch_and_stop_if_lease_refuses(incumbent, tmp_path):
    store, _, base = incumbent
    new = _candidate(store, tmp_path, "ineligible")

    lease, _ = acquire(store, "operator")
    release(store, lease)
    with pytest.raises(LeaseError, match="expired or replaced"):
        _activate(store, new, before_switch=lambda: verify_held(store, lease))
    assert machine.read_machine_current(store) == base
    assert new.pointer.machine is not None
    store.values.pop(new.pointer.machine.runs.key)
    with pytest.raises(machine.MachineMemberUnavailable, match="does not match"):
        _activate(store, new)
    assert machine.read_machine_current(store) == base
    assert store.put_keys.count(HEAD) == 1


def test_reader_reselects_after_detachment_and_web_never_needs_runs(
    incumbent,
    tmp_path,
    monkeypatch,
):
    store, old, _base = incumbent
    new = _candidate(store, tmp_path, "replacement")
    real_head = store.head
    assert old.machine is not None
    assert new.pointer.machine is not None
    old_market = old.machine.market.key
    old_runs = old.machine.runs.key
    new_runs = new.pointer.machine.runs.key
    switched = False

    def switch_during_old_validation(key):
        nonlocal switched
        if key == old_runs and not switched:
            switched = True
            outcome = _activate(store, new)
            for detached in outcome.detached:
                store.values.pop(detached)
        return real_head(key)

    with monkeypatch.context() as mocked:
        mocked.setattr(store, "head", switch_during_old_validation)
        assert machine.read_machine_current(store).pointer == new.pointer
    assert switched
    assert old_market not in store.values
    assert old_runs not in store.values
    store.head_keys.clear()
    store.get_keys.clear()
    assert machine.read_machine_current(store, roles=("market",)).pointer == new.pointer
    assert new.pointer.machine.market.key in store.head_keys
    assert new_runs not in store.head_keys + store.get_keys


@pytest.mark.parametrize("staging_damage", ["missing", "lying_metadata"])
def test_a_candidate_without_the_exact_staged_lake_cannot_become_current(
    incumbent,
    tmp_path,
    staging_damage,
):
    store, _, base = incumbent
    candidate = _candidate(store, tmp_path, "missing-graph")
    key = candidate.pointer.manifest_key
    if staging_damage == "missing":
        store.values.pop(key)
    else:
        store.store_object(key, b"not JSON", sha256=candidate.pointer.manifest_sha256)
    with pytest.raises(LakePublishError, match="manifest is not staged"):
        _activate(store, candidate)
    assert store.put_keys.count(HEAD) == 1
    assert machine.read_machine_current(store) == base


def test_same_size_wrong_member_is_not_the_current_pair(incumbent):
    store, first, _ = incumbent
    assert first.machine is not None
    runs_key = first.machine.runs.key
    stored = store.values[runs_key]
    store.store_object(runs_key, b"z" * len(stored.body))
    with pytest.raises(machine.MachineMemberUnavailable, match="runs member does not match"):
        machine.read_machine_current(store)
    assert machine.read_machine_current(store, roles=("market",)).pointer == first


def test_a_delayed_previous_detach_plan_cannot_remove_later_current(incumbent, tmp_path):
    store, first, _base = incumbent
    second = _candidate(store, tmp_path, "second")
    second_commit = _activate(store, second)
    third = _candidate(store, tmp_path, "third")
    third_commit = _activate(store, third)
    assert first.machine is not None
    assert second.pointer.machine is not None
    assert third.pointer.machine is not None
    for detached in third_commit.detached + second_commit.detached:
        store.values.pop(detached, None)
    assert sorted(key for key in store.values if key.startswith("machine/payloads/")) == sorted(
        (third.pointer.machine.market.key, third.pointer.machine.runs.key)
    )
    assert machine.read_machine_current(store) == third_commit.current


def test_explicit_store_denial_stops_without_another_head_read(incumbent, tmp_path, monkeypatch):
    store, _, _base = incumbent
    candidate = _candidate(store, tmp_path, "denied")
    store.head_keys.clear()
    store.get_keys.clear()

    def deny(*args, **kwargs):
        raise R2Error("request refused", http_status=403)

    monkeypatch.setattr(store, "put_file", deny)
    with pytest.raises(R2Error, match="request refused"):
        _activate(store, candidate)
    assert HEAD not in store.get_keys + store.head_keys


def test_old_candidate_cannot_rejoin_with_a_newer_current_base(incumbent, tmp_path):
    store, _, _ = incumbent
    older = _candidate(store, tmp_path, "older")
    old_commit = _activate(store, older)
    next_candidate = _candidate(store, tmp_path, "next")
    next_commit = _activate(store, next_candidate)
    assert old_commit.current.pointer == older.pointer
    assert next_commit.detached
    assert set(next_commit.detached) <= store.values.keys()

    # A detached but existing member may not be selected again while its cleanup is in flight.
    with pytest.raises(LakeCASConflict):
        _activate(store, older)
    for key in next_commit.detached:
        store.values.pop(key)
    assert machine.read_machine_current(store) == next_commit.current


@pytest.mark.parametrize(
    "damage", ["market-origin", "wrong-runs", "wrong-hash", "missing-selection"]
)
def test_stage_rejects_structurally_valid_wrong_members(incumbent, tmp_path, damage):
    store, _, incumbent_head = incumbent
    lake = _stage_lake(store, tmp_path, "next")
    market, runs = _files(tmp_path, lake.pointer)
    if damage == "market-origin":
        with closing(sqlite3.connect(market)) as db, db:
            write_lake_store_origin(db, LakeStoreOrigin("not-next", "f" * 64))
    else:
        runs = tmp_path / "wrong.sqlite"
        publish_machine_generation(
            runs,
            release_id="not-next" if damage == "wrong-runs" else lake.pointer.release_id,
            release_sha256="f" * 64 if damage == "wrong-hash" else lake.pointer.manifest_sha256,
            select=damage != "missing-selection",
        )
    count = len(store.put_keys)
    with pytest.raises(LakePublishError, match="selected Lake release"):
        _stage_members(store, lake=lake, market=market, runs=runs)
    assert len(store.put_keys) == count
    assert machine.read_machine_current(store) == incumbent_head


def test_current_identity_is_not_latest_asof_with_more_recent_retained_selection(
    incumbent, tmp_path
):
    store, _, _ = incumbent
    lake = _stage_lake(store, tmp_path, "backfill")
    market, runs = _files(tmp_path, lake.pointer)
    publish_machine_generation(
        runs,
        release_id="more-recent-asof",
        release_sha256="f" * 64,
        as_of=date(2026, 10, 24),
    )
    wrong = read_publication(path=runs)
    assert wrong is not None
    assert wrong.generation.generation_id == "models-more-recent-asof"
    candidate = _stage_members(store, lake=lake, market=market, runs=runs)
    commit = _activate(store, candidate)
    assert commit.current.pointer == candidate.pointer
    with pull_current_machine(store, store, directory=tmp_path) as pair:
        assert pair.runs is not None
        generation, selection = machine.machine_publication_ids(pair.current.pointer)
        assert generation == "models-backfill"
        chosen = read_publication(path=pair.runs, selection_id=selection)
        assert chosen is not None
        assert chosen.generation.generation_id == generation


class ReadStore(MemoryR2Store):
    def __init__(self):
        super().__init__()
        self.downloads = []

    def read_bytes(self, key: str) -> bytes:
        assert key != HEAD, "policy must not resolve current again"
        return self.get_bytes(key)

    def download_file(self, key, path, *, expect_bytes=None, expect_etag=None):
        self.downloads.append((key, expect_etag))
        if (stored := self.values.get(key)) is None:
            raise R2Error("detached", http_status=404)
        if expect_etag is not None and stored.etag != expect_etag:
            raise R2PinnedObjectMismatch("foreign version")
        return super().download_file(key, path, expect_bytes=expect_bytes)


def test_pull_retries_both_files_after_detached_runs_and_yields_only_second_pair(
    incumbent,
    tmp_path,
    monkeypatch,
):
    store, first, _ = incumbent
    next_candidate = _candidate(store, tmp_path, "next")
    next_pair = next_candidate.pointer.machine
    assert first.machine is not None
    assert next_pair is not None
    first_pair = first.machine
    real_download = store.download_file
    store.downloads.clear()
    started = []

    def switch(key, path, **kwargs):
        started.append(path)
        if key == first_pair.runs.key:
            commit = _activate(store, next_candidate)
            for detached in commit.detached:
                store.values.pop(detached)
        return real_download(key, path, **kwargs)

    monkeypatch.setattr(store, "download_file", switch)
    work = tmp_path / "incoming"
    with pull_current_machine(store, store, directory=work) as pair:
        assert pair.current.pointer == next_candidate.pointer
        assert pair.release.release_id == "next"
        assert pair.runs is not None
        assert read_lake_store_origin(pair.market) == LakeStoreOrigin(
            "next", next_candidate.pointer.manifest_sha256
        )
        machine.require_runs_publication(next_candidate.pointer, pair.runs)
        assert not started[0].exists()
        assert started[0].parent != pair.market.parent
    assert store.downloads == [
        (ref.key, ref.etag)
        for ref in [first_pair.market, first_pair.runs, next_pair.market, next_pair.runs]
    ]
    assert list(work.iterdir()) == []


def test_pull_unchanged_head_damage_never_exposes_any_working_files(
    incumbent,
    tmp_path,
    monkeypatch,
):
    store, first, _ = incumbent
    assert first.machine is not None
    runs_key = first.machine.runs.key
    store.values.pop(runs_key)
    store.downloads.clear()
    with (
        pytest.raises(R2Error, match="detached"),
        pull_current_machine(store, store, directory=tmp_path / "incoming"),
    ):
        pytest.fail("torn pair was yielded")
    assert [key for key, _ in store.downloads] == [first.machine.market.key, runs_key]
    assert list((tmp_path / "incoming").iterdir()) == []


def test_portfolio_gets_only_market_but_requires_complete_production_manifest(
    incumbent,
    tmp_path,
):
    store, pointer, _ = incumbent
    assert pointer.machine is not None
    store.downloads.clear()
    with pull_current_machine(store, store, directory=tmp_path, portfolio_only=True) as pair:
        assert pair.runs is None
        assert pair.release.release_id == pointer.release_id
    assert store.downloads == [(pointer.machine.market.key, pointer.machine.market.etag)]

    release = load_lake_model_json(store.get_bytes(pointer.manifest_key), ReleaseManifest)
    datasets = {
        name: item for name, item in release.datasets.items() if name != "jquants.market_calendar"
    }
    assert len(datasets) == len(release.datasets) - 1
    changed = canonical_lake_model_bytes(
        release.model_copy(
            update={
                "datasets": datasets,
                "data_as_of": min(item.data_as_of for item in datasets.values()),
            }
        )
    )
    load_lake_model_json(
        changed, ReleaseManifest
    )  # The wire is valid, but production policy is not.
    wrong = pointer.model_copy(update={"manifest_sha256": hashlib.sha256(changed).hexdigest()})
    store.store_object(pointer.manifest_key, changed)
    store.store_object(HEAD, canonical_lake_model_bytes(wrong))
    store.downloads.clear()
    with (
        pytest.raises(LakeReadError, match="fails operational policy"),
        pull_current_machine(store, store, directory=tmp_path, portfolio_only=True),
    ):
        pytest.fail("production policy was skipped for a pinned named release")
    assert store.downloads == []


def test_sealing_will_not_validate_a_wal_other_than_the_bytes_it_will_ship(incumbent, tmp_path):
    store, _, base = incumbent
    lake = _stage_lake(store, tmp_path, "fixed-new")
    market, runs = _files(tmp_path, lake.pointer)
    past = LakeStoreOrigin("still-in-the-main-file", "d" * 64)
    candidate = LakeStoreOrigin(lake.pointer.release_id, lake.pointer.manifest_sha256)
    with closing(sqlite3.connect(market)) as connection, connection:
        write_lake_store_origin(connection, past)
    with closing(sqlite3.connect(market)) as keeper:
        assert keeper.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        with keeper:
            write_lake_store_origin(keeper, candidate)
        assert read_lake_store_origin(market) == candidate
        with closing(sqlite3.connect(market.resolve().as_uri() + "?immutable=1", uri=True)) as main:
            assert main.execute("SELECT release_id FROM lake_store_origin").fetchone() == (
                past.release_id,
            )
        count = len(store.put_keys)
        with pytest.raises(LakePublishError, match="outstanding SQLite sidecar"):
            _stage_members(store, lake=lake, market=market, runs=runs)
        assert len(store.put_keys) == count
    assert machine.read_machine_current(store) == base


def test_runs_size_gate_stops_before_sending_market_or_runs(incumbent, tmp_path):
    store, _, base = incumbent
    lake = _stage_lake(store, tmp_path, "too-large")
    market, runs = _files(tmp_path, lake.pointer)
    # A sparse file exposes the exact service boundary without requiring >5 GiB of disk/downloads.
    with runs.open("r+b") as output:
        output.truncate(machine.MAX_MACHINE_PUT_BYTES + 1)
    count = len(store.put_keys)
    with pytest.raises(machine.MachineMemberTooLarge, match="R2 single PUT limit"):
        _stage_members(store, lake=lake, market=market, runs=runs)
    assert len(store.put_keys) == count
    assert machine.read_machine_current(store) == base


@pytest.mark.parametrize(
    "mode", ["at-limit", "large", "large-lost-ack", "large-denied", "large-expired"]
)
def test_only_large_fresh_runs_use_multipart_and_keep_the_same_reader(
    incumbent, tmp_path, monkeypatch, mode
):
    store, selected, base = incumbent
    lake = _stage_lake(store, tmp_path, "large-runs")
    market, runs = _files(tmp_path, lake.pointer)
    with closing(sqlite3.connect(runs)) as db, db:
        db.execute("CREATE TABLE harmless_filler (payload BLOB)")
        db.execute("INSERT INTO harmless_filler VALUES (zeroblob(?))", (market.stat().st_size,))
    size = runs.stat().st_size
    assert size > market.stat().st_size
    limit = size if mode == "at-limit" else size - 1
    monkeypatch.setattr(machine, "MAX_MACHINE_PUT_BYTES", limit)
    multipart_keys: list[str] = []
    lease_checks = 0

    def held():
        nonlocal lease_checks
        lease_checks += 1
        if mode == "large-expired" and lease_checks > 1:
            raise LeaseError("previous lease expired")

    def multipart(key, path, *, while_held, size, sha256, content_md5, content_type):
        assert key.startswith("machine/payloads/")
        assert key.endswith("/runs.sqlite")
        assert key != selected.machine.runs.key
        assert size == path.stat().st_size
        assert size > limit
        while_held()
        multipart_keys.append(key)
        if mode == "large-denied":
            raise R2Error("unauthorized without strict-stop hook", http_status=401)
        while_held()
        store.put_file(
            key,
            path,
            sha256=sha256,
            content_md5=content_md5,
            content_type=content_type,
            if_none_match=True,
        )
        store.values[key].etag = "0" * 32 + "-2"
        if mode == "large-lost-ack":
            raise R2Error("completion acknowledgement was lost")
        return store.head(key)

    monkeypatch.setattr(store, "put_immutable_multipart_file", multipart, raising=False)
    store.put_keys.clear()
    if mode != "at-limit":
        # A transport with multipart capability alone cannot authorize the new write.
        with pytest.raises(machine.MachineMemberTooLarge, match="without multipart"):
            _stage_members(store, lake=lake, market=market, runs=runs)
        assert store.put_keys == []
    if mode in {"large-denied", "large-expired"}:
        with pytest.raises(R2Error if mode == "large-denied" else LeaseError):
            _publish_machine(store, lake=lake, market=market, runs=runs, while_held=held)
        assert len(multipart_keys) == 1
        assert len(store.put_keys) == 1
        assert store.put_keys[0].endswith("/market.sqlite")
        assert store.delete_keys == []
        assert machine.read_machine_current(store) == base
        assert lease_checks == (1 if mode == "large-denied" else 2)
    elif mode == "large-lost-ack":
        with pytest.raises(machine.MachineStageInterrupted) as raised:
            _publish_machine(store, lake=lake, market=market, runs=runs, while_held=held)
        assert multipart_keys == [raised.value.unconfirmed]
        assert store.put_keys == [raised.value.detached[0], raised.value.unconfirmed]
        assert store.delete_keys == list(raised.value.detached)
        assert machine.read_machine_current(store) == base
        assert all(store.head(selected.machine.member(role).key) for role in ("market", "runs"))
    else:
        committed, retired = _publish_machine(
            store, lake=lake, market=market, runs=runs, while_held=held
        )
        assert retired
        published = committed.current.pointer
        assert store.put_keys == [published.machine.market.key, published.machine.runs.key, HEAD]
        assert multipart_keys == ([published.machine.runs.key] if mode == "large" else [])
        with pull_current_machine(store, store, directory=tmp_path / "reader") as pair:
            assert pair.current == committed.current
            assert pair.runs.read_bytes() == runs.read_bytes()
        assert (published.machine.runs.key, published.machine.runs.etag) in store.downloads
    assert lease_checks >= 1
    assert all(not key.endswith(".bak") for key in store.put_keys)


@pytest.mark.parametrize("mode", ["confirmed", "retire-refused", "ambiguous"])
def test_ordinary_publish_only_retires_exact_proven_noncurrent_keys(
    incumbent, tmp_path, monkeypatch, mode
):
    store, old, base = incumbent
    lake = _stage_lake(store, tmp_path, "next")
    market, runs = _files(tmp_path, lake.pointer)
    assert old.machine is not None
    old_keys = (old.machine.market.key, old.machine.runs.key)
    real_put, real_delete = store.put_file, store.delete_object

    def put(key, path, **kw):
        if key == HEAD and mode == "ambiguous":
            raise R2Error("write may still commit")
        return real_put(key, path, **kw)

    def delete(key):
        assert key in old_keys
        assert read_pointer_snapshot(store).pointer.release_id == "next"
        if mode == "retire-refused":
            raise R2Error("delete refused", http_status=403)
        real_delete(key)

    monkeypatch.setattr(store, "put_file", put)
    monkeypatch.setattr(store, "delete_object", delete)
    if mode == "ambiguous":
        with pytest.raises(machine.MachineSwitchUnconfirmed):
            _publish_machine(
                store,
                lake=lake,
                market=market,
                runs=runs,
                while_held=lambda: None,
            )
        assert machine.read_machine_current(store) == base
        assert not store.delete_keys
    else:
        committed, retired = _publish_machine(
            store,
            lake=lake,
            market=market,
            runs=runs,
            while_held=lambda: None,
        )
        assert machine.read_machine_current(store) == committed.current
        assert committed.detached == old_keys
        assert retired is (mode == "confirmed")
        assert old.manifest_key in store.values
        assert store.delete_keys == (list(old_keys) if retired else [])
        assert not any(key.endswith(".bak") for key in store.put_keys)


def _patch_machine_pull_transport(monkeypatch, store):
    @contextmanager
    def open_local(*, mirror, bucket):
        assert bucket == "fake-stores"
        with lake_session() as session:
            yield session, LakeObjectCache(root=mirror, source=LocalMirrorSource(mirror))

    monkeypatch.setattr(machine_pull, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(machine_pull, "open_lake", open_local)
    monkeypatch.setattr(
        snapshot_write_cli,
        "prefetching_hydration_cache",
        lambda cache, **_kwargs: nullcontext(cache),
    )


def test_daily_cli_pins_old_origin_during_the_later_hydrate(
    incumbent,
    tmp_path,
    monkeypatch,
    capsys,
):
    store, old, _ = incumbent
    new = _candidate(store, tmp_path, "later")
    _patch_machine_pull_transport(monkeypatch, store)
    handle = object()
    checks = []
    monkeypatch.setattr(machine_pull, "_load_handle", lambda path: handle)
    monkeypatch.setattr(machine_pull, "verify_held", lambda _store, item: checks.append(item))
    market = tmp_path / "work/market.sqlite"
    runs = tmp_path / "work/runs.sqlite"
    common = [
        "--market",
        str(market),
        "--mirror",
        str(tmp_path / "mirror"),
        "--bucket",
        "fake-stores",
        "--lease-handle",
        str(tmp_path / "lease"),
    ]
    assert machine_pull.main(["pull", "--mode", "daily", "--runs", str(runs), *common]) == 0
    chosen = json.loads(capsys.readouterr().out)
    assert chosen["hydrated_datasets"] == 0
    assert (chosen["generation_id"], chosen["selection_id"]) == machine.machine_publication_ids(old)
    assert read_lake_store_origin(market) == LakeStoreOrigin(old.release_id, old.manifest_sha256)
    _activate(store, new)
    store.get_keys.clear()
    store.head_keys.clear()
    # No new mutable head read in hydrate: only the verified absolute origin.
    assert machine_pull.main(["hydrate-daily", *common]) == 0
    filled = json.loads(capsys.readouterr().out)
    assert filled["release_id"] == old.release_id
    assert set(filled["rows"]) >= {"jquants.daily_bars", "jquants.market_calendar"}
    assert HEAD not in store.get_keys + store.head_keys
    assert checks
    assert all(item is handle for item in checks)


def test_working_pair_install_preserves_both_old_files_if_second_replace_fails(
    incumbent,
    tmp_path,
    monkeypatch,
    capsys,
):
    store, pointer, _ = incumbent
    _patch_machine_pull_transport(monkeypatch, store)
    market, runs = tmp_path / "work/market.sqlite", tmp_path / "work/runs.sqlite"
    market.parent.mkdir()
    market.write_bytes(b"previous market")
    runs.write_bytes(b"previous runs")
    real_replace = Path.replace

    def fail_second(source, target):
        if target == runs:
            raise OSError("simulated installation failure")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_second)
    assert (
        machine_pull.main(
            [
                "pull",
                "--mode",
                "full",
                "--market",
                str(market),
                "--runs",
                str(runs),
                "--mirror",
                str(tmp_path / "mirror"),
                "--bucket",
                "fake-stores",
            ]
        )
        == 1
    )
    assert capsys.readouterr().out == ""
    assert market.read_bytes() == b"previous market"
    assert runs.read_bytes() == b"previous runs"
    assert pointer.machine is not None
    assert store.downloads[-2:] == [
        (pointer.machine.market.key, pointer.machine.market.etag),
        (pointer.machine.runs.key, pointer.machine.runs.etag),
    ]


class _WorkerKilled(BaseException):
    """Simulate a runner interruption that cannot execute cleanup logic."""


@pytest.mark.parametrize("failure", ["delete-refused", "killed-after-cas", "killed-mid-delete"])
def test_committed_predecessor_is_recovered_before_any_further_publication(
    incumbent, tmp_path, monkeypatch, failure
):
    store, old, _ = incumbent
    assert old.machine is not None
    old_market, old_runs = old.machine.market.key, old.machine.runs.key
    second = _stage_lake(store, tmp_path, "second")
    market, runs = _files(tmp_path, second.pointer)
    real_put, real_delete = store.put_file, store.delete_object

    def put_then_stop(key, path, **kwargs):
        result = real_put(key, path, **kwargs)
        if key == HEAD and failure == "killed-after-cas":
            raise _WorkerKilled()
        return result

    def delete_then_stop(key):
        if key == old_runs:
            if failure == "killed-mid-delete":
                raise _WorkerKilled()
            raise R2OperationStopped("delete denied")
        real_delete(key)

    with monkeypatch.context() as patched:
        patched.setattr(store, "put_file", put_then_stop)
        patched.setattr(store, "delete_object", delete_then_stop)
        if failure == "delete-refused":
            committed, retired = _publish_machine(
                store, lake=second, market=market, runs=runs, while_held=lambda: None
            )
            assert not retired
            assert committed.current.pointer is not None
            assert committed.current.pointer.release_id == "second"
        else:
            with pytest.raises(_WorkerKilled):
                _publish_machine(
                    store, lake=second, market=market, runs=runs, while_held=lambda: None
                )
    assert old_runs in store.values
    assert (old_market in store.values) is (failure == "killed-after-cas")
    snapshot = machine.read_machine_current(store)
    assert snapshot.pointer is not None
    assert snapshot.pointer.machine is not None
    assert snapshot.pointer.retired_publication_id == old.machine.publication_id
    b_id = snapshot.pointer.machine.publication_id

    # A new normal run encounters real deletion denial before *any* Market Lake
    # graph or Machine PUT. B remains usable for other readers and source seals.
    lease, _ = acquire(store, "daily")
    handle = tmp_path / "handle.json"
    _save_handle(handle, lease)
    writes = len(store.put_keys)

    def still_denied(_key):
        raise R2OperationStopped("delete denied again")

    with monkeypatch.context() as patched:
        patched.setattr(store, "delete_object", still_denied)
        with pull_current_machine(store, store, directory=tmp_path) as pulled:
            assert pulled.release.release_id == "second"
            assert pulled.runs is not None
            with pytest.raises(R2OperationStopped, match="denied again"):
                models.integrate_models(
                    store=store,
                    market=pulled.market,
                    runs=pulled.runs,
                    economic=tmp_path / "economic.sqlite",
                    mirror=tmp_path / "new-mirror",
                    as_of=date(2026, 10, 24),
                    lease_handle=handle,
                    growth_start=None,
                    base=pulled.current,
                )
    assert len(store.put_keys) == writes
    assert machine.read_machine_current(store) == snapshot

    # A successful repair leaves the existing receipt byte-for-byte untouched.
    def while_held():
        verify_held(store, lease)

    ready = machine.finish_machine_retirement(store, snapshot, while_held=while_held)
    assert old_market not in store.values
    assert old_runs not in store.values
    assert machine.read_machine_current(store) == snapshot
    third = _stage_lake(store, tmp_path, "third")
    market, runs = _files(tmp_path, third.pointer)
    committed, retired = _publish_machine(
        store, lake=third, base=ready, market=market, runs=runs, while_held=while_held
    )
    assert retired
    now = machine.read_machine_current(store)
    assert now == committed.current
    assert now.pointer is not None
    assert now.pointer.machine is not None
    assert now.pointer.retired_publication_id == b_id
    assert {key for key in store.values if key.startswith("machine/payloads/")} == {
        now.pointer.machine.market.key,
        now.pointer.machine.runs.key,
    }
    assert (
        len(
            {old.manifest_key, second.pointer.manifest_key, third.pointer.manifest_key}
            & store.values.keys()
        )
        == 3
    )


def test_retirement_never_uses_a_stale_receipt_even_for_metadata_only_change(incumbent, tmp_path):
    store, first, _ = incumbent
    next_pair = _candidate(store, tmp_path, "next")
    committed = _activate(store, next_pair)
    assert first.machine is not None
    original = committed.current
    assert original.pointer is not None
    # Readers still see the very same active pair, but this is no longer the
    # exact receipt that authorized cleanup.
    changed = original.pointer.model_copy(update={"retired_publication_id": None})
    store.store_object(HEAD, canonical_lake_model_bytes(changed))
    with pytest.raises(LakeCASConflict, match="changed before completing retirement"):
        machine.finish_machine_retirement(store, original, while_held=lambda: None)
    assert not store.delete_keys
    assert machine.read_machine_current(store).pointer == changed


def test_retired_uuid_cannot_be_the_live_pair_or_an_unmanaged_key(incumbent):
    _, live, _ = incumbent
    assert live.machine is not None
    for bad in (live.machine.publication_id, "runs.sqlite.bak"):
        wire = live.model_dump(mode="json")
        wire["retired_publication_id"] = bad
        with pytest.raises(ValueError, match=r"machine retirement|String should match pattern"):
            LakeReleasePointer.model_validate(wire)


def test_stage_never_rebases_an_earlier_retirement_proof(incumbent, tmp_path):
    store, _, old_snapshot = incumbent
    stale = machine.finish_machine_retirement(store, old_snapshot, while_held=lambda: None)
    _activate(store, _candidate(store, tmp_path, "second"))
    later = _stage_lake(store, tmp_path, "third")
    market, runs = _files(tmp_path, later.pointer)
    sent = len(store.put_keys)
    with pytest.raises(LakePublishError, match="differs from the staged Lake"):
        machine.stage_machine_members(
            store,
            lake=later,
            base=stale,
            market=market,
            runs=runs,
        )
    assert len(store.put_keys) == sent


def test_different_receipt_still_selecting_candidate_never_authorizes_its_deletion(
    incumbent, tmp_path, monkeypatch
):
    store, _, _ = incumbent
    lake = _stage_lake(store, tmp_path, "concurrent-metadata")
    market, runs = _files(tmp_path, lake.pointer)
    real_put = store.put_file

    def change_receipt_metadata(key, path, **kwargs):
        ack = real_put(key, path, **kwargs)
        if key == HEAD:
            pointer = read_pointer_snapshot(store).pointer
            assert pointer is not None
            changed = pointer.model_copy(update={"retired_publication_id": "f" * 32})
            store.store_object(key, canonical_lake_model_bytes(changed))
        return ack

    monkeypatch.setattr(store, "put_file", change_receipt_metadata)
    with pytest.raises(machine.MachineSwitchUnconfirmed, match="remains selected"):
        _publish_machine(
            store,
            lake=lake,
            market=market,
            runs=runs,
            while_held=lambda: None,
        )
    assert not store.delete_keys
    pointer = machine.read_machine_current(store).pointer
    assert pointer is not None
    assert pointer.release_id == lake.pointer.release_id


def _offline_r2() -> Boto3R2Store:
    return Boto3R2Store(
        bucket="service-free",
        env={
            "R2_ACCOUNT_ID": "a" * 32,
            "R2_ACCESS_KEY_ID": "offline",
            "R2_SECRET_ACCESS_KEY": "offline",
        },
    )


@pytest.mark.parametrize("acknowledged", [True, False])
def test_absent_read_after_machine_pointer_put_never_authorizes_cleanup(
    incumbent, tmp_path, monkeypatch, acknowledged
):
    store, _, _base = incumbent
    lake = _stage_lake(store, tmp_path, "missing-current-reply")
    market, runs = _files(tmp_path, lake.pointer)
    adapter = _offline_r2()
    real_put = store.put_file
    pointer_attempted = False
    deferred_path = tmp_path / "deferred-head.json"
    deferred_options = {}

    class AmbiguousResponse:
        def get_object(self, **_):
            # The service has not proved absence. Default reads interpret the
            # service error code as missing even when HTTP says it failed.
            raise ClientError(
                {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 503}},
                "GetObject",
            )

    monkeypatch.setattr(adapter, "_client", lambda *_, **__: AmbiguousResponse())

    def put(key, path, **kwargs):
        nonlocal pointer_attempted
        if key != HEAD:
            return real_put(key, path, **kwargs)
        pointer_attempted = True
        if acknowledged:
            return real_put(key, path, **kwargs)
        deferred_path.write_bytes(path.read_bytes())
        deferred_options.update(kwargs)
        raise R2Error("pointer PUT may still commit")

    def get_checked(key, *, max_bytes):
        nonlocal pointer_attempted
        if pointer_attempted and key == HEAD:
            pointer_attempted = False
            return adapter.get_checked_object(key, max_bytes=max_bytes)
        return store.get_bytes_with_metadata(key)

    monkeypatch.setattr(store, "put_file", put)
    monkeypatch.setattr(store, "get_checked_object", get_checked, raising=False)
    with pytest.raises(LakePublishError) as raised:
        _publish_machine(store, lake=lake, market=market, runs=runs, while_held=lambda: None)
    if not acknowledged:
        real_put(HEAD, deferred_path, **deferred_options)
    assert store.delete_keys == []
    assert isinstance(raised.value, machine.MachineSwitchUnconfirmed)
    current = machine.read_machine_current(store).pointer
    assert current is not None
    assert current.release_id == lake.pointer.release_id


@pytest.mark.parametrize(
    ("status", "selected"),
    [(503, "predecessor"), (None, "predecessor"), (503, "candidate"), (503, "competitor")],
)
def test_machine_conflict_reply_only_retires_keys_after_positive_readback(
    incumbent, tmp_path, monkeypatch, status, selected
):
    store, _, base = incumbent
    lake = _stage_lake(store, tmp_path, "new-candidate")
    market, runs = _files(tmp_path, lake.pointer)
    competitor = (
        _candidate(store, tmp_path, "winning-competitor") if selected == "competitor" else None
    )
    adapter = _offline_r2()
    real_put = store.put_file
    held_body = tmp_path / "pending-pointer.json"
    held_options = {}
    submitted_preconditions = []

    class ConflictReply:
        def put_object(self, *, IfMatch, **_):
            submitted_preconditions.append(IfMatch)
            metadata = {} if status is None else {"HTTPStatusCode": status}
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": metadata},
                "PutObject",
            )

    monkeypatch.setattr(adapter, "_client", lambda *_, **__: ConflictReply())

    def put(key, path, **kwargs):
        if key != HEAD:
            return real_put(key, path, **kwargs)
        held_body.write_bytes(path.read_bytes())
        held_options.update(kwargs)
        if selected == "candidate":
            real_put(key, path, **kwargs)
        elif competitor is not None:
            with monkeypatch.context() as unpatched:
                unpatched.setattr(store, "put_file", real_put)
                _activate(store, competitor)
        return adapter.put_file(key, path, **kwargs)

    monkeypatch.setattr(store, "put_file", put)

    if selected == "candidate":
        committed, retired = _publish_machine(
            store, lake=lake, market=market, runs=runs, while_held=lambda: None
        )
        assert retired
        assert committed.current.payload == held_body.read_bytes()
        assert tuple(store.delete_keys) == committed.detached
        assert machine.read_machine_current(store) == committed.current
    else:
        with pytest.raises(LakePublishError) as raised:
            _publish_machine(store, lake=lake, market=market, runs=runs, while_held=lambda: None)
        assert isinstance(raised.value.__cause__, LakeCASConflict)
        pending = load_lake_model_json(held_body.read_bytes(), LakeReleasePointer)
        assert pending.machine is not None
        if competitor is None:
            assert read_pointer_snapshot(store) == base
            # The real conditional request can still win on the captured predecessor.
            real_put(HEAD, held_body, **held_options)
            assert store.delete_keys == []
            assert isinstance(raised.value, machine.MachineSwitchUnconfirmed)
            assert machine.read_machine_current(store).pointer == pending
        else:
            assert isinstance(raised.value, LakeCASConflict)
            assert store.delete_keys == [pending.machine.market.key, pending.machine.runs.key]
            with pytest.raises(LakeCASConflict):
                real_put(HEAD, held_body, **held_options)
            assert machine.read_machine_current(store).pointer == competitor.pointer
    assert submitted_preconditions == [f'"{base.precondition.etag}"']


@pytest.mark.parametrize("after_delete", [False, True])
def test_detached_machine_requires_http_404_before_accepting_absence(
    incumbent, tmp_path, monkeypatch, after_delete
):
    store, _old, _base = incumbent
    committed = _activate(store, _candidate(store, tmp_path, "detached-head-proof"))
    adapter = _offline_r2()
    key = committed.detached[0]
    requests = []

    class MissingOrUnavailable:
        def head_object(self, *, Bucket, Key):
            assert Bucket == "service-free"
            if Key != key:
                assert Key == committed.detached[1]
                raise ClientError(
                    {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                    "HeadObject",
                )
            if after_delete and len(requests) == 1:
                return {"ETag": '"old-body"', "ContentLength": 1}
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")

        def delete_object(self, *, Bucket, Key):
            assert Bucket == "service-free"
            assert Key == key

    def select_client(*_, single_attempt=False):
        requests.append(single_attempt)
        return MissingOrUnavailable()

    monkeypatch.setattr(adapter, "_client", select_client)
    monkeypatch.setattr(machine, "read_pointer_snapshot", lambda _: committed.current)
    with pytest.raises(R2Error, match="head-object failed"):
        machine.finish_machine_retirement(adapter, committed.current, while_held=lambda: None)
    assert requests == ([True, True, True] if after_delete else [True])


def test_detached_machine_skips_members_with_verified_http_404(incumbent, tmp_path, monkeypatch):
    store, _old, _base = incumbent
    committed = _activate(store, _candidate(store, tmp_path, "confirmed-absent"))
    adapter = _offline_r2()
    absent = []

    class Gone:
        def head_object(self, *, Bucket, Key):
            assert Bucket == "service-free"
            absent.append(Key)
            raise ClientError(
                {"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
                "HeadObject",
            )

    monkeypatch.setattr(adapter, "_client", lambda *_, **__: Gone())
    monkeypatch.setattr(machine, "read_pointer_snapshot", lambda _: committed.current)
    assert (
        machine.finish_machine_retirement(
            adapter, committed.current, while_held=lambda: None
        ).snapshot
        == committed.current
    )
    assert absent == list(committed.detached)
