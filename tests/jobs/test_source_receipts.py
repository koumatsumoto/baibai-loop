import json
from contextlib import closing

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_engine.storage.objects.r2 import (
    LakePublishError,
)
from baibai_jobs.acquisition.receipts import (
    available_handoffs,
    download_fixed,
    seal_integrated,
)


class Store(MemoryR2Store):
    def iter_objects(self, prefix):
        yield from [
            (key, len(value.body))
            for key, value in sorted(self.values.items())
            if key.startswith(prefix)
        ]


def stage(
    store, directory, stage_id, group="boj", *, started_at="2026-10-24T07:43:00+00:00", value=None
):
    directory.mkdir()
    from datetime import date, datetime

    from baibai_engine.data_collection.receipts.native_sources.records import store_snapshot

    with closing(initialize_database(directory / "receipts.sqlite")) as conn, conn:
        if value is not None:
            store_snapshot(
                conn,
                source_id="fred-payems",
                observed_at=date(2026, 10, 24),
                fetched_at=datetime.fromisoformat(started_at),
                records=[{"value": value}],
            )
    (directory / "result.json").write_text(
        json.dumps(
            {
                "group": group,
                "asof": "2026-10-24",
                "started_at": started_at,
                "sources": [],
            }
        )
    )
    save_handoff(store, stage_id, directory)


def test_integration_freezes_available_receipts_without_waiting_for_later_source(tmp_path):
    store = Store()
    stage(store, tmp_path / "a", "boj-2026-10-24-a")
    frozen = available_handoffs(store)
    # An unrelated slow source finishes only after integration's input boundary.
    stage(store, tmp_path / "b", "fred_csv-2026-10-24-b", "fred_csv")
    assert [entry["source"] for entry in frozen] == ["economic-boj"]
    download_fixed(store, frozen[0], tmp_path / "readback")
    seal_integrated(
        store, frozen[0], generations={"economic_etag": "canonical-etag"}, recovery=False
    )
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


def test_malformed_handoff_does_not_block_other_ready_source(tmp_path):
    store = Store()
    stage(store, tmp_path / "good", "boj-2026-10-24-good")
    store.store_object("coordination/economic-sources/broken/handoff.json", b"{invalid JSON")
    selected = available_handoffs(store)
    assert len(selected) == 2
    assert any(entry.get("invalid") for entry in selected)
    assert [entry["source"] for entry in selected if not entry.get("invalid")] == ["economic-boj"]


def test_noop_integration_advances_source_watermark_before_a_late_changed_stage(
    tmp_path, monkeypatch
):
    from datetime import UTC, date, datetime

    from baibai_engine.data_collection.receipts.native_sources.records import read_snapshots, store_snapshot
    from baibai_engine.data_collection.receipts.native_sources.handoff import accept_rechecks
    from baibai_engine.storage.sqlite.source_cache.schema import open_connection
    from baibai_jobs.integration.sources import apply
    from baibai_jobs.integration.native_source_merge import merge_stores

    store = Store()
    canonical = tmp_path / "canonical.sqlite"
    with closing(initialize_database(canonical)) as conn, conn:
        store_snapshot(
            conn,
            source_id="fred-payems",
            observed_at=date(2026, 10, 24),
            fetched_at=datetime(2026, 10, 24, 7, 1, tzinfo=UTC),
            records=[{"value": "A"}],
        )
    stage(
        store,
        tmp_path / "a3",
        "fred_csv-2026-10-24-a3",
        "fred_csv",
        started_at="2026-10-24T07:03:00+00:00",
        value="A",
    )
    newest = available_handoffs(store)[0]
    accepted = tmp_path / "accepted.sqlite"
    report = accept_rechecks(tmp_path / "a3/receipts.sqlite", canonical, accepted)
    assert report["accepted_receipts"] == 0
    merge_stores(accepted, canonical)
    seal_integrated(store, newest, generations={"economic_etag": "noop-integrated"}, recovery=False)
    stage(
        store,
        tmp_path / "b2",
        "fred_csv-2026-10-24-b2",
        "fred_csv",
        started_at="2026-10-24T07:02:00+00:00",
        value="B",
    )
    fixed = available_handoffs(store)
    assert len(fixed) == 1
    assert fixed[0]["late_before_integrated"] is True
    work = tmp_path / "integration"
    work.mkdir()
    (work / "plan.json").write_text(json.dumps({"handoffs": fixed}))
    market = tmp_path / "market.sqlite"
    with closing(open_connection(market)):
        pass
    monkeypatch.setattr(
        "baibai_jobs.integration.sources.extract_saved_edinet",
        lambda *args, **kwargs: {},
    )
    result = apply(store, work, market=market, economic=canonical, asof=date(2026, 10, 24))
    assert result["handoffs"][0]["recovery"] is True
    with closing(initialize_database(canonical)) as conn:
        assert next(
            read_snapshots(conn, date(2026, 10, 24), date(2026, 10, 24), source_id="fred-payems")
        ).records == ({"value": "A"},)
