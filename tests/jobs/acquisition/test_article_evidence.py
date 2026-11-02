"""Read the same private original before/after integration, without a fictional release."""

import hashlib
import json
from contextlib import closing
from datetime import timedelta

import pytest
from tests.helpers.article_acquisition import SLOT, _packet, receipt
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.articles.store import (
    read_body,
    read_receipt,
    retain_receipt,
)
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.article_evidence import export_evidence
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.integration.article_handoff import import_handoff


@pytest.mark.parametrize("conditional", [False, True])
def test_private_exact_export_keeps_original_after_integration(tmp_path, conditional):
    store = MemoryR2Store()
    packet = _packet(tmp_path)
    original = receipt(body=b"<rss><channel/></rss>")
    with closing(open_connection(packet / "receipts.sqlite")) as conn, conn:
        retain_receipt(conn, original, body=b"<rss><channel/></rss>")
        if conditional:
            original = original.model_copy(
                update={
                    "receipt_id": "conditional",
                    "status": "not_modified",
                    "http_status": 304,
                    "received_at": original.received_at + timedelta(minutes=1),
                }
            )
            retain_receipt(conn, original)
    report = {
        "source": original.source,
        "started_at": SLOT.isoformat(),
        "asof": SLOT.date().isoformat(),
        "receipt_ids": [original.receipt_id],
        "projection_kinds": {original.receipt_id: "index"},
    }
    (packet / "result.json").write_text(json.dumps(report))
    save_handoff(store, "fixed", packet, namespace="article-sources")
    manifest = store.values["coordination/article-sources/fixed/handoff.json"].body
    digest = hashlib.sha256(manifest).hexdigest()
    before = {key: value.body for key, value in store.values.items()}
    exported = tmp_path / "exported"
    result = export_evidence(
        store,
        exported,
        source=original.source,
        stage_id="fixed",
        manifest_sha256=digest,
        receipt_digests={original.receipt_id: original.sha256},
    )
    assert result["receipts"] == 1
    evidence = json.loads((exported / "evidence.json").read_text())
    assert "lineage_release_id" not in evidence["stage"]
    assert "explicit_article_recovery" not in evidence["stage"]
    assert (exported / "body-0.bin").read_bytes() == b"<rss><channel/></rss>"
    assert {key: value.body for key, value in store.values.items()} == before
    database = tmp_path / "market.sqlite"
    assert import_handoff(original.source, database, packet).complete
    with closing(connect_read_only(database)) as conn:
        same = read_receipt(conn, original.receipt_id, receipt_sha256=original.sha256)
        assert same == original
        assert same.body_ref is not None
        assert read_body(conn, same.body_ref) == (exported / "body-0.bin").read_bytes()
    with pytest.raises(FileExistsError):
        export_evidence(
            store,
            exported,
            source=original.source,
            stage_id="fixed",
            manifest_sha256=digest,
            receipt_digests={original.receipt_id: original.sha256},
        )
    with pytest.raises(ValueError, match="exact reference"):
        export_evidence(
            store,
            tmp_path / "mismatch",
            source=original.source,
            stage_id="fixed",
            manifest_sha256=digest,
            receipt_digests={original.receipt_id: "f" * 64},
        )
