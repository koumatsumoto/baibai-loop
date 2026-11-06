"""Metadata transport is bounded; selection and failure semantics are unchanged."""

import json
import time
from contextlib import closing, contextmanager
from datetime import date
from threading import Event, Lock

import pytest
from tests.engine.machine_analysis.generation.test_economic_fixed_inputs import (
    CLOCK,
    current,
    freeze,
    root,
)
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.native_sources.confirmations import (
    confirm_source_response,
)
from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.machine_analysis.generation.economic_inputs import rebuild_economic_input
from baibai_engine.storage.objects.r2 import LakePublishError


def seed_months(path):
    with closing(initialize_database(path)) as connection, connection:
        confirm_source_response(
            connection,
            source_id="fred-payems",
            fetched_at=CLOCK,
            records_by_day={
                date(year, month, 1): [{"PAYEMS": str(100000 + month)}]
                for year in (2023, 2024, 2025)
                for month in range(1, 13)
            },
        )


class ConcurrentStore(MemoryR2Store):
    def __init__(self):
        super().__init__()
        self.lock = Lock()
        self.overlap = Event()
        self.active = self.peak = 0

    @contextmanager
    def request(self):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.active >= 2:
                self.overlap.set()
        try:
            self.overlap.wait(timeout=2)
            time.sleep(0.001)
            yield
        finally:
            with self.lock:
                self.active -= 1

    def head(self, key):
        with self.request():
            return super().head(key)

    def get_bytes(self, key):
        with self.request():
            return super().get_bytes(key)


def test_block_requests_overlap_with_bounded_workers_and_exact_warm_results(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed_months(path)
    original = path.read_bytes()
    store = ConcurrentStore()
    saved = freeze(store, path)
    assert len(root(store, saved)["months"]) == 3
    assert 2 <= store.peak <= 8
    expected = current(path)
    store.peak = 0
    assert rebuild_economic_input(store, path, saved).context == expected
    assert 2 <= store.peak <= 8
    retained = {key: value.body for key, value in store.values.items()}
    puts = len(store.put_keys)
    assert freeze(store, path) == saved
    assert rebuild_economic_input(store, path, saved).context == expected
    assert len(store.put_keys) == puts
    assert {key: value.body for key, value in store.values.items()} == retained
    assert path.read_bytes() == original


def test_failed_block_does_not_publish_root_and_same_input_reuses_partial_objects(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed_months(path)
    original = path.read_bytes()

    class FailedBlock(MemoryR2Store):
        failed = True

        def put_file(self, key, path, **kwargs):
            body = json.loads(path.read_bytes())
            if self.failed and body.get("records") and body["records"][0]["key"][1] == "2024-01-01":
                raise LakePublishError("one block write failed")
            return super().put_file(key, path, **kwargs)

    store = FailedBlock()
    with pytest.raises(LakePublishError, match="block write"):
        freeze(store, path)
    assert store.values
    assert not any(json.loads(value.body).get("format") for value in store.values.values())
    partial = {key: value.body for key, value in store.values.items()}
    store.failed = False
    saved = freeze(store, path)
    reference = MemoryR2Store()
    assert freeze(reference, path) == saved
    assert {key: value.body for key, value in store.values.items()} == {
        key: value.body for key, value in reference.values.items()
    }
    assert all(store.values[key].body == body for key, body in partial.items())
    assert rebuild_economic_input(store, path, saved).context == current(path)
    assert path.read_bytes() == original


def test_prefetched_corrupt_block_still_stops_cold_rebuild(tmp_path):
    path = tmp_path / "economic.sqlite"
    seed_months(path)
    store = MemoryR2Store()
    saved = freeze(store, path)
    digest = list(root(store, saved)["months"].values())[1]
    store.store_object("fixed-inputs/economic/v1/" + digest + ".json", b"{}")
    original = path.read_bytes()
    with pytest.raises(LakePublishError, match="missing or changed"):
        rebuild_economic_input(store, path, saved)
    assert path.read_bytes() == original
