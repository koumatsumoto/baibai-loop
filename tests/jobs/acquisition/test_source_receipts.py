import json
from contextlib import closing

import pytest
from tests.helpers.native_handoff import stage
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.native_sources.store import initialize_database
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    R2Error,
)
from baibai_jobs.acquisition.receipts import (
    available_handoffs,
    download_fixed,
    seal_integrated,
)


def test_integration_freezes_available_receipts_without_waiting_for_later_source(tmp_path):
    store = MemoryR2Store()
    stage(store, tmp_path / "a", "boj-2026-10-24-a")
    frozen = available_handoffs(store)
    # An unrelated slow source finishes only after integration's input boundary.
    stage(store, tmp_path / "b", "fred_csv-2026-10-24-b", "fred_csv")
    assert [entry["source"] for entry in frozen] == ["economic-boj"]
    store.get_keys.clear()
    download_fixed(store, frozen[0], tmp_path / "readback")
    assert sum(key.endswith("/handoff.json") for key in store.get_keys) == 1
    seal_integrated(
        store, frozen[0], generations={"economic_etag": "canonical-etag"}, recovery=False
    )
    assert [entry["source"] for entry in available_handoffs(store)] == ["economic-fred_csv"]


def test_fixed_receipt_manifest_cannot_be_replaced_between_plan_and_apply(tmp_path):
    store = MemoryR2Store()
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
    store = MemoryR2Store()
    stage(store, tmp_path / "a", "boj-2026-10-24-a")
    stage(store, tmp_path / "b", "fred_csv-2026-10-24-b", "fred_csv")
    store.get_keys.clear()
    selected = available_handoffs(store, source="economic-boj", include_integrated=True)
    assert [entry["source"] for entry in selected] == ["economic-boj"]
    assert not any("fred_csv" in key for key in store.get_keys)


def test_malformed_handoff_does_not_block_other_ready_source(tmp_path):
    store = MemoryR2Store()
    stage(store, tmp_path / "good", "boj-2026-10-24-good")
    store.store_object("coordination/economic-sources/broken/handoff.json", b"{invalid JSON")
    selected = available_handoffs(store)
    assert len(selected) == 2
    assert any(entry.get("invalid") for entry in selected)
    assert [entry["source"] for entry in selected if not entry.get("invalid")] == ["economic-boj"]


def test_retired_scheduled_jquants_market_handoffs_do_not_read_remote_objects():
    store = MemoryR2Store()
    market_id = "jquants-market-2026-04-15-123-1"
    financial_id = "jquants-financial-2024-02-29-456-2"
    market = "coordination/market-sources"
    store.store_object(f"{market}/{market_id}/handoff.json", b"{invalid JSON")
    store.store_object(
        f"{market}/{financial_id}/handoff.json",
        b'{"result":{"source":"jquants-financial","started_at":"2024-02-29T09:00:00Z",'
        b'"asof":"2024-02-29"}}',
    )
    store.store_object(f"{market}/{financial_id}/integrated.json", b'{"status":"integrated"}')
    assert available_handoffs(store, market=True, include_integrated=True) == []
    assert not store.get_keys
    assert not store.head_keys
    assert not store.put_keys

    economic = f"coordination/economic-sources/{market_id}"
    store.store_object(
        f"{economic}/handoff.json",
        b'{"result":{"group":"boj","started_at":"2026-04-15T09:00:00Z","asof":"2026-04-15"}}',
    )
    (entry,) = available_handoffs(store)
    assert entry["source"] == "economic-boj"
    assert store.get_keys == [f"{economic}/handoff.json"]
    assert f"{economic}/integrated.json" in store.head_keys


def test_market_handoff_listing_keeps_opaque_ids_and_reports_current_malformed_keys():
    store = MemoryR2Store()
    prefix = "coordination/market-sources"
    manual = "jquants-market-2026-04-15-123-1-manual"
    broken_ids = {"jpx-earnings-2026-04-15-123-1", "jquants-financial-2026-02-29-123-1"}
    store.store_object(
        f"{prefix}/{manual}/handoff.json",
        b'{"result":{"source":"jpx-earnings","started_at":"2026-04-15T09:00:00Z",'
        b'"asof":"2026-04-15"}}',
    )
    for stage_id in broken_ids:
        store.store_object(f"{prefix}/{stage_id}/handoff.json", b"{invalid JSON")

    entries = {entry["stage_id"]: entry for entry in available_handoffs(store, market=True)}
    assert set(entries) == broken_ids | {manual}
    assert entries[manual]["source"] == "jpx-earnings"
    assert all(entries[stage_id].get("invalid") for stage_id in broken_ids)
    assert set(store.get_keys) == {f"{prefix}/{stage_id}/handoff.json" for stage_id in entries}
    assert f"{prefix}/{manual}/integrated.json" in store.head_keys


def test_noop_integration_advances_source_watermark_before_a_late_changed_stage(
    tmp_path, monkeypatch
):
    from datetime import UTC, date, datetime

    from tests.helpers.native_handoff import integrate_economic

    from baibai_engine.data_collection.receipts.native_sources.handoff import accept_rechecks
    from baibai_engine.data_collection.receipts.native_sources.records import (
        read_snapshots,
        store_snapshot,
    )
    from baibai_jobs.integration.native_source_merge import merge_stores

    store = MemoryR2Store()
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
    result = integrate_economic(store, work / "economic", canonical, date(2026, 10, 24))
    assert result["handoffs"][0]["recovery"] is True
    with closing(initialize_database(canonical)) as conn:
        assert next(
            read_snapshots(conn, date(2026, 10, 24), date(2026, 10, 24), source_id="fred-payems")
        ).records == ({"value": "A"},)


@pytest.mark.parametrize(
    "error",
    [
        R2Error("transport unavailable"),
        LakeCASConflict("conditional generation changed"),
        LakePublishError("R2 is only text in this source-contract error"),
    ],
)
def test_handoff_plan_uses_error_type_not_message_for_failure_scope(tmp_path, monkeypatch, error):
    import baibai_jobs.acquisition.receipts as receipts

    store = MemoryR2Store()
    stage(store, tmp_path / "a", "boj-fault")
    stage(store, tmp_path / "b", "fred_csv-good", "fred_csv")
    read = receipts.read_write_once_private

    def fail_one(selected_store, key):
        if key == "coordination/economic-sources/boj-fault/handoff.json":
            raise error
        return read(selected_store, key)

    monkeypatch.setattr(receipts, "read_write_once_private", fail_one)
    if type(error) is not LakePublishError:
        with pytest.raises(type(error)):
            available_handoffs(store)
    else:
        entries = available_handoffs(store)
        assert [entry["invalid"] for entry in entries if entry.get("invalid")] == [
            "LakePublishError"
        ]
        assert [entry["source"] for entry in entries if not entry.get("invalid")] == [
            "economic-fred_csv"
        ]


def test_receipt_reads_are_parallel_and_remain_deterministic(tmp_path, monkeypatch):
    from threading import Barrier

    import baibai_jobs.acquisition.receipts as receipts

    store = MemoryR2Store()
    stage(store, tmp_path / "first", "boj-2026-10-24-first")
    stage(store, tmp_path / "second", "fred_csv-2026-10-24-second", "fred_csv")
    barrier = Barrier(2, timeout=3)
    original = receipts.read_write_once_private

    def synchronized_read(target, key):
        if key.endswith("/handoff.json"):
            barrier.wait()
        return original(target, key)

    monkeypatch.setattr(receipts, "read_write_once_private", synchronized_read)
    items = available_handoffs(store)
    assert [item["source"] for item in items] == ["economic-boj", "economic-fred_csv"]
    assert all(not item.get("invalid") for item in items)


@pytest.mark.parametrize("market", [False, True])
@pytest.mark.parametrize(
    "damage",
    [
        {"manifest_sha256": "0" * 64},
        {"source": "other"},
        {"started_at": "2026-11-20T08:01:00Z"},
        {"started_at": "2026-11-20T08:02:00"},
        {"started_at": None},
        {"status": "unknown"},
        {"status": []},
    ],
)
def test_invalid_terminal_cannot_consume_or_mark_another_handoff_late(market, damage):
    store = MemoryR2Store()
    namespace = "market-sources" if market else "economic-sources"
    source = "jpx-earnings" if market else "boj"
    other = "jpx-regulations" if market else "fred_csv"

    def handoff(owner, suffix):
        key = f"coordination/{namespace}/{owner}-{suffix}/handoff.json"
        body = {"source" if market else "group": owner, "asof": "2026-11-20"}
        body["started_at"] = f"2026-11-20T08:{suffix}:00Z"
        store.store_object(key, json.dumps({"result": body}).encode())
        return key

    handoff(source, "01")
    key = handoff(source, "02")
    handoff(other, "03")
    fixed = {item["stage_id"]: item for item in available_handoffs(store, market=market)}
    entry = fixed[f"{source}-02"]
    seal_integrated(store, entry, generations={}, recovery=False)
    key = key.removesuffix("handoff.json") + "integrated.json"
    corrected = store.values[key].body
    marker = json.loads(corrected)
    store.store_object(key, json.dumps({**marker, **damage}).encode())
    store.put_keys.clear()
    observed = {item["stage_id"]: item for item in available_handoffs(store, market=market)}
    assert observed[f"{source}-02"]["invalid"]
    sibling = observed[f"{source}-01"]
    assert sibling["invalid"] == "unverified_source_marker"
    assert not sibling.get("late_before_integrated")
    assert not observed[f"{other}-03"].get("invalid")
    assert not observed[f"{other}-03"]["late_before_integrated"]
    with pytest.raises(LakePublishError):
        seal_integrated(store, entry, generations={}, recovery=False)
    assert not store.put_keys

    # A different valid timezone representation of the same start is not a conflict.
    equivalent = json.dumps({**marker, "started_at": "2026-11-20T17:02:00+09:00"}).encode()
    store.store_object(key, equivalent)
    seal_integrated(store, entry, generations={}, recovery=False)
    assert not store.put_keys
    assert store.values[key].body == equivalent
    # Only an actually verified seal can prove that this sibling is late.
    repaired = {item["stage_id"]: item for item in available_handoffs(store, market=market)}
    assert f"{source}-02" not in repaired
    assert repaired[f"{source}-01"]["late_before_integrated"]


@pytest.mark.parametrize("status", ["integrated", "needs_explicit_recovery"])
@pytest.mark.parametrize("market", [False, True])
def test_authenticated_first_format_terminal_still_resolves_without_rewriting(market, status):
    store = MemoryR2Store()
    namespace = "market-sources" if market else "economic-sources"
    key = f"coordination/{namespace}/first-format"
    result = {"source" if market else "group": "example", "asof": "2026-10-24"}
    result["started_at"] = "2026-10-24T10:00:00Z"
    store.store_object(key + "/handoff.json", json.dumps({"result": result}).encode())
    (entry,) = available_handoffs(store, market=market)
    marker = {
        "manifest_sha256": entry["manifest_sha256"],
        "canonical_generations": {},
        "status": status,
        "integrated_at": "2026-10-24T11:00:00+00:00",
    }
    store.store_object(key + "/integrated.json", json.dumps(marker).encode())
    assert available_handoffs(store, market=market) == []
    (saved,) = available_handoffs(store, market=market, include_integrated=True)
    assert saved["integration"] == marker
    seal_integrated(store, entry, generations={}, recovery=status != "integrated")
    assert not store.put_keys
