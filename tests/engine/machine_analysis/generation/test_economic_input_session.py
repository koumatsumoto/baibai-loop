"""A normal retain/rebuild pair reuses only its own verified immutable metadata."""

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
from tests.engine.machine_analysis.generation.test_economic_fixed_inputs import (
    ASOF,
    EVALUATED,
    current,
    freeze,
    root,
    run,
    seed,
)
from tests.engine.machine_analysis.generation.test_economic_fixed_transport import seed_months
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.tradingview_market import capture

from baibai_engine.data_collection.providers.tradingview.market_receipts import (
    retain_market_capture,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.generation import economic_inputs as inputs
from baibai_engine.machine_analysis.generation.runners.reading_rules import DEFAULT_RULES_PATH
from baibai_engine.storage.objects.json import MAX_PRIVATE_OBJECT_BYTES
from baibai_engine.storage.objects.r2 import LakePublishError, R2Error


def retain(session, path, **kwargs):
    return session.retain(path, as_of=ASOF, evaluation_time=EVALUATED, **kwargs)


def assert_closed(session):
    assert session._state == "closed"
    assert session._saved_json is None
    assert session._objects._payloads == {}
    assert session._objects._bytes == 0


def test_session_preserves_identity_context_and_halves_warm_metadata_reads(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed_months(path)
    run(path, "latest-failed")
    with closing(initialize_database(path)) as connection:
        retain_market_capture(connection, capture(receipt_id="failed", error=True))
    before = path.read_bytes()
    store = MemoryR2Store()
    saved = freeze(store, path)
    expected = inputs.rebuild_economic_input(store, path, saved)
    count = len(root(store, saved)["months"]) + 1
    store.head_keys.clear()
    store.get_keys.clear()
    store.put_keys.clear()
    session = inputs.EconomicInputSession(store)
    assert retain(session, path) == saved
    assert len(store.head_keys) == len(store.get_keys) == count
    assert session.rebuild(path, saved) == expected
    assert len(store.head_keys) == len(store.get_keys) == count
    assert not store.put_keys
    assert path.read_bytes() == before
    assert_closed(session)
    with pytest.raises(ValueError, match="once"):
        session.rebuild(path, saved)
    with pytest.raises(ValueError, match="retains once"):
        retain(session, path)
    # Another call has to authenticate and verify the same remote objects anew.
    later = inputs.EconomicInputSession(store)
    assert later.rebuild(path, retain(later, path)) == expected
    assert len(store.head_keys) == len(store.get_keys) == count * 2


@pytest.mark.parametrize("capacity", [0, 1, 2500])
def test_zero_tiny_or_partial_capacity_uses_normal_reads_without_changing_results(
    tmp_path, monkeypatch, capacity
):
    path = tmp_path / "economic.sqlite"
    seed_months(path)
    store = MemoryR2Store()
    saved = freeze(store, path)
    expected = inputs.rebuild_economic_input(store, path, saved)
    monkeypatch.setattr(inputs, "_MAX_RETAINED_BYTES", capacity)
    session = inputs.EconomicInputSession(store)
    assert retain(session, path) == saved
    cached = dict(session._objects._payloads)
    count = len(root(store, saved)["months"]) + 1
    if capacity == 2500:
        assert 0 < len(cached) < count
    assert session._objects._bytes == sum(map(len, cached.values())) <= capacity
    store.head_keys.clear()
    store.get_keys.clear()
    assert session.rebuild(path, saved) == expected
    assert len(store.head_keys) == len(store.get_keys) == count - len(cached)
    assert_closed(session)


def test_read_miss_does_not_evict_or_admit_and_index_has_a_bound(monkeypatch):
    store = MemoryR2Store()
    first = {"fixture": "one"}
    second = {"fixture": "two"}
    third = {"fixture": "three"}
    monkeypatch.setattr(inputs, "_MAX_RETAINED_OBJECTS", 1)
    objects = inputs._RetainedObjects(store, max_bytes=1000)
    one = objects.retain(first)
    two = objects.retain(second)
    three = inputs._retain(store, third)
    before = dict(objects._payloads)
    assert set(before) == {one}
    assert hashlib.sha256(objects.read(two)).hexdigest() == two
    assert hashlib.sha256(objects.read(three)).hexdigest() == three
    assert objects._payloads == before
    store.head_keys.clear()
    store.get_keys.clear()
    assert hashlib.sha256(objects.read(one)).hexdigest() == one
    assert not store.head_keys
    assert not store.get_keys


@pytest.mark.parametrize("remote_damage", ["none", "metadata", "read-error"])
def test_evicted_block_is_read_remotely_and_remote_failure_is_not_hidden(
    tmp_path, monkeypatch, remote_damage
):
    path = tmp_path / "economic.sqlite"
    seed(path)
    store = MemoryR2Store()
    session = inputs.EconomicInputSession(store)
    saved = retain(session, path)
    digest = next(iter(root(store, saved)["months"].values()))
    objects = session._objects
    objects._bytes -= len(objects._payloads.pop(digest))
    store.head_keys.clear()
    store.get_keys.clear()
    if remote_damage == "metadata":
        store.store_object(inputs._key(digest), b"{}")
        with pytest.raises(LakePublishError, match="missing or changed"):
            session.rebuild(path, saved)
        assert not store.get_keys
    elif remote_damage == "read-error":

        def fail_read(key):
            store.get_keys.append(key)
            raise R2Error("remote quota stop")

        monkeypatch.setattr(store, "get_bytes", fail_read)
        with pytest.raises(R2Error, match="remote quota stop"):
            session.rebuild(path, saved)
        assert store.get_keys == [inputs._key(digest)]
    else:
        assert session.rebuild(path, saved).context == current(path)
        assert store.get_keys == [inputs._key(digest)]
    assert store.head_keys == [inputs._key(digest)]
    assert_closed(session)


def test_new_session_cannot_hide_changed_remote_bytes_with_prior_session_cache(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed(path)
    store = MemoryR2Store()
    first = inputs.EconomicInputSession(store)
    saved = retain(first, path)
    first.rebuild(path, saved)
    digest = next(iter(root(store, saved)["months"].values()))
    store.store_object(inputs._key(digest), b"{}")
    second = inputs.EconomicInputSession(store)
    with pytest.raises(LakePublishError, match="missing or changed"):
        retain(second, path)
    assert_closed(second)


@pytest.mark.parametrize("damage", ["empty", "large", "different"])
def test_corrupt_cached_bytes_stop_without_remote_repair(tmp_path, damage):
    path = tmp_path / "economic.sqlite"
    seed(path)
    store = MemoryR2Store()
    session = inputs.EconomicInputSession(store)
    saved = retain(session, path)
    digest = saved.descriptor["selection_sha256"]
    session._objects._payloads[digest] = (
        b""
        if damage == "empty"
        else b"x" * (MAX_PRIVATE_OBJECT_BYTES + 1)
        if damage == "large"
        else b"{}"
    )
    store.head_keys.clear()
    store.get_keys.clear()
    with pytest.raises(LakePublishError, match=r"retained.*failed integrity"):
        session.rebuild(path, saved)
    assert not store.head_keys
    assert not store.get_keys
    assert_closed(session)


def test_same_digest_from_another_store_cannot_use_previous_verified_bytes():
    first, second = MemoryR2Store(), MemoryR2Store()
    one = inputs._RetainedObjects(first, max_bytes=1000)
    two = inputs._RetainedObjects(second, max_bytes=1000)
    digest = one.retain({"fixture": "one"})
    with pytest.raises(LakePublishError, match="missing or changed"):
        two.read(digest)
    assert second.head_keys == [inputs._key(digest)]
    assert not second.get_keys
    assert one.read(digest) == first.values[inputs._key(digest)].body


@pytest.mark.parametrize("damage", ["reference", "descriptor"])
def test_session_rejects_another_fixed_identity_and_clears_its_cache(tmp_path, damage):
    path = tmp_path / "economic.sqlite"
    seed(path)
    store = MemoryR2Store()
    session = inputs.EconomicInputSession(store)
    saved = retain(session, path)
    if damage == "reference":
        other = saved.model_copy(update={"reference": "another-fixed-identity"})
    else:
        other = saved
        other.descriptor["format"] = "economic-input-v1"
    with pytest.raises(ValueError, match="retained identity once"):
        session.rebuild(path, other)
    assert_closed(session)
    with pytest.raises(ValueError, match="once"):
        session.rebuild(path, saved)


@pytest.mark.parametrize("operation", ["retain", "rebuild"])
def test_session_rejects_out_of_sequence_calls_and_releases_cached_objects(tmp_path, operation):
    path = tmp_path / "economic.sqlite"
    seed(path)
    store = MemoryR2Store()
    saved = freeze(store, path)
    session = inputs.EconomicInputSession(store)
    if operation == "retain":
        retain(session, path)
        with pytest.raises(ValueError, match="retains once"):
            retain(session, path)
    else:
        with pytest.raises(ValueError, match="retained identity once"):
            session.rebuild(path, saved)
    assert_closed(session)


def test_partial_retain_failure_clears_cache_and_retry_must_read_remote_objects(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed_months(path)

    class FailedRoot(MemoryR2Store):
        failed = True

        def put_file(self, key, path, **kwargs):
            if self.failed and json.loads(path.read_bytes()).get("format"):
                raise LakePublishError("root write failed")
            return super().put_file(key, path, **kwargs)

    store = FailedRoot()
    failed = inputs.EconomicInputSession(store)
    with pytest.raises(LakePublishError, match="root write failed"):
        retain(failed, path)
    assert len(store.values) == 3
    assert_closed(failed)
    with pytest.raises(ValueError, match="retains once"):
        retain(failed, path)
    store.failed = False
    store.get_keys.clear()
    fresh = inputs.EconomicInputSession(store)
    saved = retain(fresh, path)
    assert len(store.get_keys) == 4
    assert fresh.rebuild(path, saved).context == current(path)
    assert len(store.get_keys) == 4
    assert_closed(fresh)


@pytest.mark.parametrize("damage", ["record", "run", "document", "recipe"])
def test_session_still_checks_original_receipts_and_recipe_after_retain(tmp_path, damage):
    path = tmp_path / "economic.sqlite"
    seed(path)
    run(path, "failed")
    with closing(initialize_database(path)) as connection:
        retain_market_capture(connection, capture())
    rules = tmp_path / DEFAULT_RULES_PATH.name
    rules.write_bytes(DEFAULT_RULES_PATH.read_bytes())
    store = MemoryR2Store()
    session = inputs.EconomicInputSession(store)
    saved = retain(session, path, rules_path=rules)
    if damage == "recipe":
        rules.write_text(rules.read_text() + "\n# changed\n")
    else:
        table, column = {
            "record": ("records", "record_count"),
            "run": ("runs", "record_count"),
            "document": ("documents", "metadata"),
        }[damage]
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute(f"UPDATE economic_source_{table} SET {column}='999'")
    before = path.read_bytes()
    with pytest.raises(ValueError, match=r"recipe differs|failed integrity"):
        session.rebuild(path, saved, rules_path=rules)
    assert path.read_bytes() == before
    assert_closed(session)
