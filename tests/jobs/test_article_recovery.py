import hashlib
import json
from contextlib import closing
from dataclasses import replace
from datetime import timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.article_acquisition import SLOT, _packet, receipt

from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.article_planning import (
    load_discovery_cursor,
    load_planning,
    publish_planning_input,
    source_key,
)
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.external_assessments.staging import put_private
from baibai_jobs.acquisition.receipts import download_fixed, seal_integrated
from baibai_jobs.maintenance.article_recovery import exact_entry, prepare, register_discovery


def test_exact_recovery_keeps_original_marker_manifest_and_clock(tmp_path):
    store = MemoryR2Store()
    packet = _packet(tmp_path)
    save_handoff(store, "late", packet, namespace="article-sources")
    manifest = store.values["coordination/article-sources/late/handoff.json"].body
    digest = hashlib.sha256(manifest).hexdigest()
    entry = exact_entry(store, source="boj_news", stage_id="late", manifest_sha256=digest)
    seal_integrated(store, entry, generations={"lake_release_id": "before"}, recovery=True)
    marker = store.values["coordination/article-sources/late/integrated.json"].body
    prepare(
        store, tmp_path / "recovery", source="boj_news", stage_id="late", manifest_sha256=digest
    )
    plan = json.loads((tmp_path / "recovery/plan.json").read_text())
    assert plan["handoffs"][0]["started_at"] == SLOT.isoformat()
    download_fixed(store, entry, tmp_path / "raw")
    assert (tmp_path / "raw/receipts.sqlite").read_bytes() == (
        packet / "receipts.sqlite"
    ).read_bytes()
    seal_integrated(store, entry, generations={"lake_release_id": "after"}, recovery=False)
    assert store.values["coordination/article-sources/late/integrated.json"].body == marker
    assert store.values["coordination/article-sources/late/handoff.json"].body == manifest
    assert f"coordination/article-sources/late/recovered-{digest}.json" in store.values
    with pytest.raises(LakePublishError, match="differs"):
        exact_entry(store, source="boj_news", stage_id="late", manifest_sha256="f" * 64)
    with pytest.raises(ValueError, match="article source"):
        exact_entry(store, source="edinet-list", stage_id="late", manifest_sha256=digest)


@pytest.mark.parametrize("last_slot", [None, SLOT.isoformat()])
def test_discovery_registration_is_idempotent_and_preserves_live_source_state(tmp_path, last_slot):
    store = MemoryR2Store()
    packet = _packet(tmp_path, "mof_news")
    body = b"synthetic retained original"
    original = receipt(body=body)
    with closing(open_connection(packet / "receipts.sqlite")) as conn, conn:
        retain_receipt(conn, original, body=body)
    original_report = {
        "source": "mof_news",
        "started_at": SLOT.isoformat(),
        "asof": SLOT.date().isoformat(),
        "receipt_ids": [original.receipt_id],
        "projection_kinds": {original.receipt_id: "document"},
    }
    (packet / "result.json").write_text(json.dumps(original_report))
    save_handoff(store, "old-mof", packet, namespace="article-sources")
    key = "coordination/article-sources/old-mof/handoff.json"
    old_manifest = store.values[key].body
    digest = hashlib.sha256(old_manifest).hexdigest()
    publish_planning_input(store, "mof_news", {"lake_release_id": "current"})
    state, etag = load_planning(store, "mof_news")
    state = replace(
        state,
        last_slot=last_slot,
        not_before=(SLOT + timedelta(hours=8)).isoformat(),
        cursor={"pending_urls": ["https://www.mof.go.jp/other.htm"]},
    )
    put_private(store, source_key("mof_news"), state.encoded(), previous_etag=etag)
    result = register_discovery(
        store,
        tmp_path / "recovery",
        source="mof_news",
        stage_id="old-mof",
        manifest_sha256=digest,
        receipt_ids=[original.receipt_id],
    )
    assert result["status"] == "registered"
    after, _ = load_planning(store, "mof_news")
    assert after.last_slot == state.last_slot
    assert after.not_before == state.not_before
    assert after.requests == state.requests
    cursor = load_discovery_cursor(store, after)
    assert cursor["pending_urls"] == state.cursor["pending_urls"]
    recovered = next(iter(cursor["discoveries"].values()))
    assert recovered["receipt_id"] == original.receipt_id
    assert recovered["receipt_sha256"] == original.sha256
    assert recovered["body_ref"] == original.body_ref.model_dump()
    assert recovered["stage"] == result["stage"]
    writes = list(store.put_keys)
    repeated = register_discovery(
        store,
        tmp_path / "repeat",
        source="mof_news",
        stage_id="old-mof",
        manifest_sha256=digest,
        receipt_ids=[original.receipt_id],
    )
    assert repeated == {"status": "already_registered", "registered": 0}
    assert store.put_keys == writes
    assert store.values[key].body == old_manifest


def test_discovery_recovery_rejects_index_evidence_from_another_source(tmp_path):
    store = MemoryR2Store()
    packet = _packet(tmp_path, "mof_news")
    original = receipt()
    foreign = receipt("foreign-index").model_copy(update={"source": "boj_news"})
    with closing(open_connection(packet / "receipts.sqlite")) as conn, conn:
        retain_receipt(conn, original, body=b"synthetic")
        retain_receipt(conn, foreign, body=b"synthetic")
    report = {
        "source": "mof_news",
        "started_at": SLOT.isoformat(),
        "asof": SLOT.date().isoformat(),
        "receipt_ids": [original.receipt_id, foreign.receipt_id],
        "projection_kinds": {original.receipt_id: "document", foreign.receipt_id: "index"},
        "document_evidence": {original.receipt_id: {"index_receipt_id": foreign.receipt_id}},
    }
    (packet / "result.json").write_text(json.dumps(report))
    save_handoff(store, "mixed-source", packet, namespace="article-sources")
    digest = hashlib.sha256(
        store.values["coordination/article-sources/mixed-source/handoff.json"].body
    ).hexdigest()
    publish_planning_input(store, "mof_news", {})
    writes = list(store.put_keys)
    with pytest.raises(ValueError, match="index differs from its source"):
        register_discovery(
            store,
            tmp_path / "recovery",
            source="mof_news",
            stage_id="mixed-source",
            manifest_sha256=digest,
            receipt_ids=[original.receipt_id],
        )
    assert store.put_keys == writes
