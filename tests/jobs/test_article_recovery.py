import hashlib
import json

import pytest

from tests.helpers.r2_store import MemoryR2Store
from tests.jobs.test_article_planning import SLOT, _packet

from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.receipts import download_fixed, seal_integrated
from baibai_jobs.maintenance.article_recovery import exact_entry, prepare


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
