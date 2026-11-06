"""L2 readback seals its own work without an acquisition-planning write."""

import json

import pytest
from tests.helpers.article_acquisition import SLOT, _packet
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.r2 import LakePublishError
from baibai_jobs.acquisition.article_planning import (
    commit_stage,
    load_planning,
    publish_planning_input,
    source_key,
)
from baibai_jobs.acquisition.receipts import available_handoffs
from baibai_jobs.integration import sources


@pytest.mark.parametrize("failure", [None, "readback", "lease_after_readback", "not_applied"])
def test_article_seal_requires_readback_but_never_acknowledges_l1(tmp_path, monkeypatch, failure):
    store = MemoryR2Store()
    publish_planning_input(store, "boj_news", {})
    state, etag = load_planning(store, "boj_news")
    entry = commit_stage(
        store,
        state=state,
        expected_etag=etag,
        slot=SLOT,
        stage_id="boj_news-seal",
        directory=_packet(tmp_path),
        requests={},
        cursor={},
        not_before=None,
    )
    work = tmp_path / "integration"
    work.mkdir()
    (work / "applied.json").write_text(
        json.dumps(
            {
                "handoffs": [
                    {
                        "entry": entry,
                        "applied": failure != "not_applied",
                        "article_delta": {"articles.source_receipts": {"receipt": "digest"}},
                        "recovery": False,
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(sources, "Boto3R2Store", lambda **_: store)
    monkeypatch.setattr(sources, "_load_handle", lambda _: object())
    monkeypatch.setattr(sources, "canonical_generations", lambda _: {"lake_release_id": "fixed"})
    calls = []
    planning = store.values[source_key("boj_news")]

    def held(*_):
        calls.append("lease")
        if failure == "lease_after_readback" and "readback" in calls:
            raise LakePublishError("lease lost after exact readback")

    def readback(_store, **kwargs):
        calls.append("readback")
        assert kwargs["generations"] == {"lake_release_id": "fixed"}
        assert kwargs["delta"] == {"articles.source_receipts": {"receipt": "digest"}}
        if failure == "readback":
            raise LakePublishError("published raw differs")
        # An independent L1 writer can advance during L2 publication. Its state
        # must not be read, CAS-updated, or replaced by the L2 seal.
        value = {**json.loads(planning.body), "not_before": "2026-10-31T00:00:00+00:00"}
        store.store_object(source_key("boj_news"), json.dumps(value).encode())
        return {"articles.source_receipts": 1}

    monkeypatch.setattr(sources, "verify_held", held)
    monkeypatch.setattr(sources, "verify_delta", readback)
    store.get_keys.clear()
    store.put_keys.clear()
    result = sources.main(
        [
            "seal",
            "--directory",
            str(work),
            "--lease-handle",
            str(tmp_path / "lease.json"),
        ]
    )
    assert result == int(failure in {"readback", "lease_after_readback"})
    assert source_key("boj_news") not in store.get_keys
    assert source_key("boj_news") not in store.put_keys
    assert calls == (
        ["lease"]
        if failure == "not_applied"
        else ["lease", "readback"]
        if failure == "readback"
        else ["lease", "readback", "lease"]
    )
    ready = available_handoffs(store, market=True, namespace="article-sources")
    assert len(ready) == int(failure is not None)
    if failure in {None, "lease_after_readback"}:
        assert json.loads(store.values[source_key("boj_news")].body)["not_before"] == (
            "2026-10-31T00:00:00+00:00"
        )
    else:
        assert store.values[source_key("boj_news")] == planning
