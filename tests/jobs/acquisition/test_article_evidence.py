"""Read a saved original without opening its integration marker or changing R2."""

import hashlib
import json
from contextlib import closing
from datetime import timedelta

import pytest
from tests.helpers.article_evidence import SLOT, make_packet, receipt
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.articles.store import (
    retain_receipt,
)
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition.article_evidence import export_evidence
from baibai_jobs.acquisition.economic_staging import save_handoff


@pytest.mark.parametrize("conditional", [False, True])
@pytest.mark.parametrize("packet_schema", [49, 50])
@pytest.mark.parametrize("terminal", [None, "integrated"])
def test_private_exact_export_keeps_original(tmp_path, conditional, packet_schema, terminal):
    store = MemoryR2Store()
    packet = make_packet(tmp_path)
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
        conn.execute(f"PRAGMA user_version={packet_schema}")
    report = {
        "source": original.source,
        "started_at": SLOT.isoformat(),
        "asof": SLOT.date().isoformat(),
        "receipt_ids": [original.receipt_id],
        "projection_kinds": {original.receipt_id: "index"},
    }
    (packet / "result.json").write_text(json.dumps(report))
    save_handoff(store, "fixed", packet, namespace="article-sources")
    prefix = "coordination/article-sources/fixed"
    manifest = store.values[f"{prefix}/handoff.json"].body
    digest = hashlib.sha256(manifest).hexdigest()
    if terminal:
        store.store_object(
            f"{prefix}/integrated.json",
            json.dumps(
                {
                    "source": original.source,
                    "started_at": SLOT.isoformat(),
                    "manifest_sha256": digest,
                    "status": terminal,
                    "canonical_generations": {
                        "lake_release_id": "retained",
                        "lake_manifest_sha256": "a" * 64,
                    },
                }
            ).encode(),
        )
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
    assert evidence["scope"] == "native_originals_only"
    assert evidence["stage"]["started_at"] == SLOT.isoformat()
    assert evidence["receipts"][0]["receipt_sha256"] == original.sha256
    assert (exported / "body-0.bin").read_bytes() == b"<rss><channel/></rss>"
    assert {key: value.body for key, value in store.values.items()} == before
    assert f"{prefix}/integrated.json" not in store.get_keys + store.head_keys
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
