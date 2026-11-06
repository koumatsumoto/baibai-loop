"""Retained planning roots expose loss without another index or L2 acknowledgement."""

import hashlib
import json

import pytest
from tests.helpers.article_acquisition import SLOT, _packet
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.storage.objects.r2 import LakeCASConflict
from baibai_jobs.acquisition import article_planning
from baibai_jobs.acquisition.article_planning import (
    ArticlePlanning,
    commit_stage,
    load_planning,
    pending_handoffs,
    publish_planning_input,
    source_key,
)
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.receipts import seal_integrated


def commit(store, root, name, source="boj_news"):
    root.mkdir()
    state, etag = load_planning(store, source)
    return commit_stage(
        store,
        state=state,
        expected_etag=etag,
        slot=SLOT,
        stage_id=f"{source}-{name}",
        directory=_packet(root, source),
        requests={},
        cursor={},
        not_before=None,
    )


def remove_packet(store, entry):
    for key in list(store.values):
        if key.startswith(f"coordination/article-sources/{entry['stage_id']}/"):
            del store.values[key]


def manifest(store, entry):
    return json.loads(
        store.values[f"coordination/article-sources/{entry['stage_id']}/handoff.json"].body
    )


@pytest.mark.parametrize("sealed", [False, True])
@pytest.mark.parametrize("all_packets", [False, True])
def test_missing_accepted_original_is_visible_even_when_sealed_or_listing_is_empty(
    tmp_path, sealed, all_packets
):
    store = MemoryR2Store()
    publish_planning_input(store, "boj_news", {})
    first = commit(store, tmp_path / "first", "first")
    latest = commit(store, tmp_path / "latest", "latest")
    if sealed:
        seal_integrated(store, first, generations={}, recovery=False)
        seal_integrated(store, latest, generations={}, recovery=False)
    remove_packet(store, first)
    if all_packets:
        remove_packet(store, latest)
    (missing,) = [entry for entry in pending_handoffs(store) if entry.get("invalid")]
    assert missing["stage_id"] == (latest if all_packets else first)["stage_id"]
    assert missing["invalid"] == "missing_article_original"


@pytest.mark.parametrize("version", ["article-planning-v1", "article-planning-v2"])
def test_legacy_pending_is_seeded_once_without_rewriting_its_originals(tmp_path, version):
    from baibai_jobs.acquisition.article_discovery import DISCOVERY_REF_FORMAT

    store = MemoryR2Store()
    entries = []
    for index in range(3):
        root = tmp_path / str(index)
        root.mkdir()
        stage_id = f"boj_news-legacy-{index}"
        save_handoff(store, stage_id, _packet(root), namespace="article-sources")
        saved = store.values[f"coordination/article-sources/{stage_id}/handoff.json"].body
        entries.append(
            {
                "source": "boj_news",
                "market": True,
                "namespace": "article-sources",
                "stage_id": stage_id,
                "manifest_sha256": hashlib.sha256(saved).hexdigest(),
                "started_at": SLOT.isoformat(),
                "asof": SLOT.date().isoformat(),
            }
        )
    old_objects = dict(store.values)
    legacy = {**ArticlePlanning("boj_news", {}).to_json(), "format": version, "pending": entries}
    legacy.pop("last_stage")
    if version == "article-planning-v2":
        legacy["cursor"] = {"format": DISCOVERY_REF_FORMAT, "stage": entries[-1]}
    store.store_object(source_key("boj_news"), json.dumps(legacy).encode())
    first = commit(store, tmp_path / "first", "first")
    next_entry = commit(store, tmp_path / "next", "next")
    assert manifest(store, first)["result"]["planning"]["legacy_pending"] == entries
    assert manifest(store, first)["result"]["planning"]["previous_stage"] is None
    assert "legacy_pending" not in manifest(store, next_entry)["result"]["planning"]
    assert manifest(store, next_entry)["result"]["planning"]["previous_stage"] == first
    assert all(store.values[key] == value for key, value in old_objects.items())
    assert not any(entry.get("invalid") for entry in pending_handoffs(store))
    remove_packet(store, entries[0])
    (missing,) = [entry for entry in pending_handoffs(store) if entry.get("invalid")]
    assert missing["stage_id"] == entries[0]["stage_id"]


@pytest.mark.parametrize(
    "field", ["source", "namespace", "market", "manifest_sha256", "missing_parent"]
)
def test_root_and_parent_references_are_checked_before_seal_filtering(tmp_path, field):
    store = MemoryR2Store()
    publish_planning_input(store, "boj_news", {})
    first = commit(store, tmp_path / "first", "first")
    latest = commit(store, tmp_path / "latest", "latest")
    value = manifest(store, latest)
    planning = value["result"]["planning"]
    if field == "missing_parent":
        del planning["previous_stage"]
    else:
        planning["previous_stage"][field] = {
            "source": "mof_news",
            "namespace": "market-sources",
            "market": False,
            "manifest_sha256": "0" * 64,
        }[field]
    body = json.dumps(value).encode()
    store.store_object(f"coordination/article-sources/{latest['stage_id']}/handoff.json", body)
    latest = {**latest, "manifest_sha256": hashlib.sha256(body).hexdigest()}
    state = json.loads(store.values[source_key("boj_news")].body)
    state["last_stage"] = latest
    store.store_object(source_key("boj_news"), json.dumps(state).encode())
    seal_integrated(store, first, generations={}, recovery=False)
    seal_integrated(store, latest, generations={}, recovery=False)
    assert any(entry.get("invalid") for entry in pending_handoffs(store))


@pytest.mark.parametrize(
    ("source", "other_source"), [("boj_news", "mof_news"), ("mof_news", "boj_news")]
)
@pytest.mark.parametrize("sealed", [False, True])
def test_reference_deduplication_preserves_source_validation(
    tmp_path, source, other_source, sealed
):
    store = MemoryR2Store()
    publish_planning_input(store, source, {})
    actual = commit(store, tmp_path / "actual", "actual", source)
    if sealed:
        seal_integrated(store, actual, generations={}, recovery=False)
    wrong = {**actual, "source": other_source}
    store.store_object(
        source_key(other_source), ArticlePlanning(other_source, {}, last_stage=wrong).encoded()
    )
    (invalid,) = [entry for entry in pending_handoffs(store) if entry.get("invalid")]
    assert invalid == {**wrong, "invalid": "article_original_differs"}


def test_listing_race_does_not_invent_missing_new_root(tmp_path):
    class RacingStore(MemoryR2Store):
        after_list = None

        def iter_objects(self, prefix):
            yield from list(super().iter_objects(prefix))
            if self.after_list is not None and prefix == "coordination/article-sources/":
                action, self.after_list = self.after_list, None
                action()

    store = RacingStore()
    publish_planning_input(store, "boj_news", {})
    first = commit(store, tmp_path / "first", "first")
    store.after_list = lambda: commit(store, tmp_path / "later", "later")
    (entry,) = pending_handoffs(store)
    assert entry["stage_id"] == first["stage_id"]
    assert not entry.get("invalid")
    assert load_planning(store, "boj_news")[0].last_stage["stage_id"] == "boj_news-later"
    assert len(pending_handoffs(store)) == 2


def test_cas_loser_keeps_its_actual_parent_but_does_not_claim_root_coverage(tmp_path, monkeypatch):
    store = MemoryR2Store()
    publish_planning_input(store, "boj_news", {})
    first = commit(store, tmp_path / "first", "first")
    original = article_planning.save_handoff

    def race(remote, stage_id, directory, *, namespace):
        original(remote, stage_id, directory, namespace=namespace)
        if stage_id == "boj_news-loser":
            commit(store, tmp_path / "winner", "winner")

    monkeypatch.setattr(article_planning, "save_handoff", race)
    with pytest.raises(LakeCASConflict):
        commit(store, tmp_path / "loser", "loser")
    loser = {"stage_id": "boj_news-loser"}
    assert manifest(store, loser)["result"]["planning"]["previous_stage"] == first
    assert load_planning(store, "boj_news")[0].last_stage["stage_id"] == "boj_news-winner"
    assert len(pending_handoffs(store)) == 3
    remove_packet(store, loser)
    # The old pending protocol also could not track a failed, unpublished CAS.
    assert not any(entry.get("invalid") for entry in pending_handoffs(store))
