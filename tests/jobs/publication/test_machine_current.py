"""The current head switches a complete pair, or leaves the preceding pair readable."""

import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager, nullcontext
from datetime import date
from pathlib import Path

import pytest
from tests.helpers.lake_release import lake as lake
from tests.helpers.lake_release import market_store
from tests.helpers.model_generation import publish_machine_generation
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.screening.selection_query import read_publication
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    R2Error,
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
from baibai_jobs.publication import machine_current as machine
from baibai_jobs.publication import machine_pull, snapshot_write_cli
from baibai_jobs.publication.datasets import LAKE_DATASETS
from baibai_jobs.publication.lease import LeaseError, acquire, release, verify_held
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


def _candidate(store, tmp_path, name):
    lake = _stage_lake(store, tmp_path, name)
    market, runs = _files(tmp_path, lake.pointer)
    return machine.stage_machine_members(store, lake=lake, market=market, runs=runs)


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
        store.values.pop(detached)
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
        machine.stage_machine_members(store, lake=lake, market=market, runs=runs)
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
    candidate = machine.stage_machine_members(store, lake=lake, market=market, runs=runs)
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
            machine.stage_machine_members(store, lake=lake, market=market, runs=runs)
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
        machine.stage_machine_members(store, lake=lake, market=market, runs=runs)
    assert len(store.put_keys) == count
    assert machine.read_machine_current(store) == base


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
            machine.publish_staged_machine(
                store,
                lake=lake,
                market=market,
                runs=runs,
                while_held=lambda: None,
            )
        assert machine.read_machine_current(store) == base
        assert not store.delete_keys
    else:
        committed, retired = machine.publish_staged_machine(
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
