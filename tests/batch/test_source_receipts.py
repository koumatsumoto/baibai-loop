import json
from contextlib import closing

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_batch.storage.economic_staging import save_handoff
from baibai_batch.storage.lake_publish import LakePublishError
from baibai_batch.storage.source_receipts import available_handoffs, download_fixed, seal_integrated
from baibai_engine.collection.economic.store import initialize_database


class Store(MemoryR2Store):
    def iter_objects(self, prefix):
        yield from [(key, len(value.body)) for key, value in sorted(self.values.items()) if key.startswith(prefix)]


def stage(store, directory, stage_id, group="boj"):
    directory.mkdir()
    with closing(initialize_database(directory / "receipts.sqlite")):
        pass
    (directory / "result.json").write_text(json.dumps({
        "group": group, "asof": "2026-10-24", "started_at": "2026-10-24T07:43:00+00:00", "sources": [],
    }))
    save_handoff(store, stage_id, directory)


def test_integration_freezes_available_receipts_without_waiting_for_later_source(tmp_path):
    store = Store()
    stage(store, tmp_path / "a", "boj-2026-10-24-a")
    frozen = available_handoffs(store)
    # An unrelated slow source finishes only after integration's input boundary.
    stage(store, tmp_path / "b", "fred_csv-2026-10-24-b", "fred_csv")
    assert [entry["source"] for entry in frozen] == ["economic-boj"]
    download_fixed(store, frozen[0], tmp_path / "readback")
    seal_integrated(store, frozen[0], generations={"economic_etag": "canonical-etag"}, recovery=False)
    assert [entry["source"] for entry in available_handoffs(store)] == ["economic-fred_csv"]


def test_fixed_receipt_manifest_cannot_be_replaced_between_plan_and_apply(tmp_path):
    store = Store()
    stage(store, tmp_path / "a", "boj-2026-10-24-a")
    frozen = available_handoffs(store)
    key = "coordination/economic-sources/boj-2026-10-24-a/handoff.json"
    manifest = json.loads(store.values[key].body)
    manifest["result"]["asof"] = "2026-10-25"
    store.store_object(key, json.dumps(manifest).encode())
    with pytest.raises(LakePublishError, match="manifest changed"):
        download_fixed(store, frozen[0], tmp_path / "refused")
    assert not (tmp_path / "refused").exists()


def test_source_resume_listing_does_not_read_unrelated_receipt_manifests(tmp_path):
    store = Store()
    stage(store, tmp_path / "a", "boj-2026-10-24-a")
    stage(store, tmp_path / "b", "fred_csv-2026-10-24-b", "fred_csv")
    store.get_keys.clear()
    selected = available_handoffs(store, source="economic-boj", include_integrated=True)
    assert [entry["source"] for entry in selected] == ["economic-boj"]
    assert not any("fred_csv" in key for key in store.get_keys)
