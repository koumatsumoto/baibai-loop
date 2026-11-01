import hashlib
import json
from contextlib import closing
from dataclasses import replace
from datetime import timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store
from tests.helpers.article_acquisition import SLOT, Response, Session, receipt

import baibai_jobs.acquisition.article_discovery as worksets
from baibai_engine.data_collection.providers.articles.contracts import NativeLink
from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt
from baibai_engine.data_collection.receipts.articles.store import retain_receipt
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition import articles
from baibai_jobs.acquisition.article_discovery import DiscoveryWork
from baibai_jobs.acquisition.article_planning import (
    commit_stage,
    load_discovery_cursor,
    load_planning,
    publish_planning_input,
    source_key,
)
from baibai_jobs.acquisition.economic_staging import save_handoff
from baibai_jobs.acquisition.external_assessments.staging import put_private
from baibai_jobs.maintenance.article_recovery import register_discovery


def test_parent_completion_and_child_admission_are_one_state_change(monkeypatch):
    work = DiscoveryWork({})
    parent = receipt()
    identity = work.register(parent, {"stage_id": "raw"})
    before = json.loads(json.dumps(work.wire({})))
    links = (NativeLink("/meta-refresh", "https://www.mof.go.jp/next.htm"),)
    monkeypatch.setattr(worksets, "MAX_DISCOVERY_BYTES", 1)
    assert work.admit(identity, links, [], revision="r1") is None
    assert work.wire({}) == before
    monkeypatch.setattr(worksets, "MAX_DISCOVERY_BYTES", 1_000_000)
    queue = work.admit(identity, links, [], revision="r1")
    assert queue == [links[0].url]
    assert identity not in work.pending
    assert work.children[links[0].url][0]["parent"]["receipt_sha256"] == parent.sha256


def test_same_url_new_original_is_new_discovery_but_exact_replay_is_not():
    work = DiscoveryWork({})
    first = work.register(receipt(), {"stage_id": "first"})
    assert work.register(receipt(), {"stage_id": "copy"}) == first
    second = work.register(receipt("second", b"changed"), {"stage_id": "second"})
    assert first != second
    assert len(work.pending) == 2


def test_pending_discovery_and_children_survive_validator_eviction(tmp_path):
    from tests.helpers.article_acquisition import _packet

    remote = MemoryR2Store()
    publish_planning_input(remote, "mof_news", {})
    state, etag = load_planning(remote, "mof_news")
    work = DiscoveryWork({})
    identity = work.register(receipt(), {"stage_id": "discovery-stage"})
    work.unsupported(identity, "r1")
    urls = [f"https://www.mof.go.jp/pending-{index}.htm" for index in range(160)]
    entry = commit_stage(
        remote,
        state=state,
        expected_etag=etag,
        slot=SLOT,
        stage_id="discovery-stage",
        directory=_packet(tmp_path, "mof_news"),
        requests={},
        cursor=work.wire({"pending_urls": urls}),
        not_before=None,
    )
    saved, etag = load_planning(remote, "mof_news")
    evicted = replace(saved, requests={})
    put_private(remote, source_key("mof_news"), evicted.encoded(), previous_etag=etag)
    loaded = load_discovery_cursor(remote, evicted)
    assert loaded["pending_urls"] == urls
    assert loaded["discoveries"][identity]["stage"] == entry
    assert loaded["discoveries"][identity]["receipt_id"] == "original"


def test_discovery_cas_failure_preserves_parent_to_child_workset_for_exact_recovery(
    tmp_path, monkeypatch
):
    from tests.helpers.article_acquisition import _packet

    remote = MemoryR2Store()
    publish_planning_input(remote, "mof_news", {})
    state, etag = load_planning(remote, "mof_news")
    key = source_key("mof_news")
    before = remote.values[key].body
    work = DiscoveryWork({})
    identity = work.register(receipt(), {"stage_id": "cas-stage"})
    target = "https://www.mof.go.jp/target.htm"
    queue = work.admit(identity, (NativeLink("/meta", target),), [], revision="r1")
    original_put = remote.put_file

    def conflicting_put(key, path, **kwargs):
        if key == source_key("mof_news"):
            raise LakeCASConflict("synthetic concurrent planning update")
        return original_put(key, path, **kwargs)

    monkeypatch.setattr(remote, "put_file", conflicting_put)
    with pytest.raises(LakeCASConflict, match="concurrent planning"):
        commit_stage(
            remote,
            state=state,
            expected_etag=etag,
            slot=SLOT,
            stage_id="cas-stage",
            directory=_packet(tmp_path, "mof_news"),
            requests={},
            cursor=work.wire({"pending_urls": queue}),
            not_before=None,
        )
    assert remote.values[key].body == before
    saved = json.loads(remote.values["coordination/article-sources/cas-stage/handoff.json"].body)
    cursor = saved["result"]["planning"]["cursor"]
    assert cursor["discoveries"] == {}
    assert cursor["pending_urls"] == [target]
    assert cursor["discovery_children"][target][0]["parent"]["receipt_id"] == "original"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_lost_discovery_workset_is_not_replaced_by_a_fresh_get(tmp_path, damage):
    store = MemoryR2Store()
    publish_planning_input(store, "boj_news", {})
    session = Session([Response(200, b"<rss><channel /></rss>")])
    articles.acquire_source(
        store,
        source="boj_news",
        slot=SLOT,
        identity="first",
        directory=tmp_path / "first",
        session=session,
    )
    state, _ = load_planning(store, "boj_news")
    key = f"coordination/article-sources/{state.cursor['stage']['stage_id']}/handoff.json"
    if damage == "missing":
        store.values.pop(key)
    else:
        store.store_object(key, b"corrupt")
    with pytest.raises(LakePublishError, match="manifest differs"):
        articles.acquire_source(
            store,
            source="boj_news",
            slot=SLOT + timedelta(hours=4),
            identity="second",
            directory=tmp_path / "second",
            session=session,
        )
    assert len(session.urls) == 1


def test_unsupported_discovery_keeps_raw_and_does_not_stop_later_document(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    monkeypatch.setitem(
        articles.SOURCES,
        "mof_news",
        replace(articles.SOURCES["mof_news"], max_revalidate_bodies=0),
    )
    bad_url = "https://www.mof.go.jp/unknown.htm"
    good_url = "https://www.mof.go.jp/supported.htm"
    target = "https://www.mof.go.jp/discovered.htm"
    bad_body = b"<body>Original with an unsupported structure</body>"
    feed = (
        f"<rss><channel><item><link>{bad_url}</link></item>"
        f"<item><link>{good_url}</link></item></channel></rss>"
    ).encode()
    remote = MemoryR2Store()
    publish_planning_input(remote, "mof_news", {"lake_release_id": "before"})
    first = Session(
        [Response(200, feed), Response(200, bad_body), Response(200, b"<main>Supported</main>")]
    )
    report = articles.acquire_source(
        remote,
        source="mof_news",
        slot=SLOT,
        identity="first",
        directory=tmp_path / "first",
        session=first,
    )
    assert first.urls == [articles.SOURCES["mof_news"].entry_url, bad_url, good_url]
    assert report["failures"] == ["discovery_pending"]
    state, _ = load_planning(remote, "mof_news")
    cursor = load_discovery_cursor(remote, state)
    original = next(iter(cursor["discoveries"].values()))
    assert original["url"] == bad_url
    assert cursor["pending_urls"] == []
    second = Session([Response(200, feed)])
    same_revision = articles.acquire_source(
        remote,
        source="mof_news",
        slot=SLOT + timedelta(hours=4),
        identity="same-revision",
        directory=tmp_path / "same",
        session=second,
    )
    assert same_revision["discovery_replays"] == 0
    assert second.urls == [articles.SOURCES["mof_news"].entry_url]

    previous_discover = articles.discover_links

    def revised(source, body, **kwargs):
        if body == bad_body:
            return (NativeLink("/supported-new-layout", target),)
        return previous_discover(source, body, **kwargs)

    monkeypatch.setattr(articles, "discover_links", revised)
    monkeypatch.setattr(articles, "discovery_revision_for", lambda _: "synthetic-r2")
    third = Session([Response(200, feed), Response(200, b"<main>New target</main>")])
    replayed = articles.acquire_source(
        remote,
        source="mof_news",
        slot=SLOT + timedelta(hours=8),
        identity="new-revision",
        directory=tmp_path / "new",
        session=third,
    )
    assert replayed["discovery_replays"] == 1
    assert replayed["discovery_pending_count"] == 0
    assert third.urls == [articles.SOURCES["mof_news"].entry_url, target]
    with closing(connect_read_only(tmp_path / "new/receipts.sqlite")) as conn:
        old = json.loads(
            conn.execute(
                "SELECT receipt_json FROM article_source_receipts WHERE receipt_id = ?",
                (original["receipt_id"],),
            ).fetchone()[0]
        )
    assert ArticleReceipt.model_validate(old).sha256 == original["receipt_sha256"]
    private = json.loads((tmp_path / "new/result.json").read_text())
    edge = next(iter(private["discovery_edges"].values()))[0]
    assert edge["parent"]["receipt_id"] == original["receipt_id"]
    assert "discovery_edges" not in replayed


def test_registered_legacy_original_resumes_same_slot_without_index_get(tmp_path, monkeypatch):
    from tests.helpers.article_acquisition import _packet

    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    remote = MemoryR2Store()
    publish_planning_input(remote, "mof_news", {})
    state, etag = load_planning(remote, "mof_news")
    state = replace(state, last_slot=SLOT.isoformat())
    put_private(remote, source_key("mof_news"), state.encoded(), previous_etag=etag)
    packet = _packet(tmp_path, "mof_news")
    target = "https://www.mof.go.jp/target.htm"
    body = b"synthetic old redirect"
    original = receipt(body=body)
    with closing(open_connection(packet / "receipts.sqlite")) as conn, conn:
        retain_receipt(conn, original, body=body)
    (packet / "result.json").write_text(
        json.dumps(
            {
                "source": "mof_news",
                "started_at": SLOT.isoformat(),
                "asof": SLOT.date().isoformat(),
                "receipt_ids": [original.receipt_id],
                "projection_kinds": {original.receipt_id: "document"},
            }
        )
    )
    save_handoff(remote, "legacy", packet, namespace="article-sources")
    manifest = remote.values["coordination/article-sources/legacy/handoff.json"].body
    register_discovery(
        remote,
        tmp_path / "registration",
        source="mof_news",
        stage_id="legacy",
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        receipt_ids=[original.receipt_id],
    )
    native = articles.discover_links

    def discover(source, data, **kwargs):
        if data == body:
            return (NativeLink("/meta-refresh", target),)
        return native(source, data, **kwargs)

    monkeypatch.setattr(articles, "discover_links", discover)
    session = Session([Response(200, b"<main>Target body</main>")])
    result = articles.acquire_source(
        remote,
        source="mof_news",
        slot=SLOT,
        identity="same-slot",
        directory=tmp_path / "resumed",
        session=session,
    )
    assert result["discovery_replays"] == 1
    assert result["discovery_pending_count"] == 0
    assert result["pages"] == 0
    assert result["bodies"] == 1
    assert session.urls == [target]
    with closing(connect_read_only(tmp_path / "resumed/receipts.sqlite")) as conn:
        kept = conn.execute(
            "SELECT receipt_json FROM article_source_receipts WHERE receipt_id=?",
            (original.receipt_id,),
        ).fetchone()[0]
    assert ArticleReceipt.model_validate_json(kept).sha256 == original.sha256
    repeat = Session([])
    skipped = articles.acquire_source(
        remote,
        source="mof_news",
        slot=SLOT,
        identity="complete",
        directory=tmp_path / "complete",
        session=repeat,
    )
    assert skipped["status"] == "not_due"
    assert repeat.urls == []


def test_same_slot_pending_documents_continue_without_repoll(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    monkeypatch.setitem(
        articles.SOURCES, "mof_news", replace(articles.SOURCES["mof_news"], max_bodies=1)
    )
    remote = MemoryR2Store()
    publish_planning_input(remote, "mof_news", {})
    feed = b"<rss><channel><item><link>https://www.mof.go.jp/a.htm</link></item><item><link>https://www.mof.go.jp/b.htm</link></item></channel></rss>"
    first = Session([Response(200, feed), Response(200, b"<main>A</main>")])
    articles.acquire_source(
        remote,
        source="mof_news",
        slot=SLOT,
        identity="first",
        directory=tmp_path / "first",
        session=first,
    )
    second = Session([Response(200, b"<main>B</main>")])
    result = articles.acquire_source(
        remote,
        source="mof_news",
        slot=SLOT,
        identity="second",
        directory=tmp_path / "second",
        session=second,
    )
    assert second.urls == ["https://www.mof.go.jp/b.htm"]
    assert result["pages"] == 0
    assert result["pending_url_count"] == 0
