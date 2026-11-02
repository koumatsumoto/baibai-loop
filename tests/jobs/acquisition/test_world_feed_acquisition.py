"""World-only budgets and exact immutable article replay; no live network."""

from contextlib import closing
from datetime import timedelta

from tests.helpers.article_acquisition import SLOT, Response, Session
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.providers.articles.contracts import SOURCES, WORLD_NEWS_SOURCES
from baibai_engine.data_collection.receipts.articles.store import read_body, read_receipt
from baibai_engine.machine_analysis.observations.articles.store import read_observations
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_jobs.acquisition import articles
from baibai_jobs.acquisition.article_planning import publish_planning_input
from baibai_jobs.integration.article_handoff import import_handoff

FEED = b'''<rss><channel><item><guid>one</guid><link>https://example.invalid/one</link>
<title>Example</title><description>Summary</description></item></channel></rss>'''


def test_world_group_does_not_reduce_existing_group_budgets():
    assert set(articles.GROUPS["world"]) == WORLD_NEWS_SOURCES
    assert set(articles.GROUPS["public"]) == {
        "nhk_news",
        "tsr_data",
        "tsr_flash",
        "boj_news",
        "mof_news",
    }
    assert articles.GROUP_BYTES == {
        "world": 4 * 1024 * 1024,
        "public": 80 * 1024 * 1024,
        "minkabu": 24 * 1024 * 1024,
        "rakuten": 24 * 1024 * 1024,
    }
    assert sum(len(SOURCES[source].entry_urls) for source in WORLD_NEWS_SOURCES) == 4


def test_bbc_index_replay_and_later_correction_keep_original_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    store = MemoryR2Store()
    publish_planning_input(store, "bbc_news", {})
    database = tmp_path / "market.sqlite"
    references = []
    for index, body in enumerate((FEED, FEED.replace(b"Summary", b"Corrected"))):
        packet = tmp_path / f"packet-{index}"
        session = Session([Response(200, body), Response(200, body)])
        report = articles.acquire_source(
            store,
            source="bbc_news",
            slot=SLOT + timedelta(hours=4 * index),
            identity=f"test-{index}",
            directory=packet,
            session=session,
        )
        assert report["status"] == "completed"
        assert report["bodies"] == 0
        assert session.urls == list(SOURCES["bbc_news"].entry_urls)
        imported = import_handoff("bbc_news", database, packet)
        assert imported.complete
        replayed = import_handoff("bbc_news", database, packet)
        assert replayed.report["observation_ids"] == imported.report["observation_ids"]
        references.extend(imported.report["observation_ids"])
    with closing(connect_read_only(database)) as conn:
        saved = read_observations(conn, references)
        assert [item.text for item in saved] == ["Summary", "Summary", "Corrected", "Corrected"]
        assert len({item.article_id for item in saved}) == 1
        assert len({item.receipt_id for item in saved}) == 4
        for item in saved:
            receipt = read_receipt(conn, item.receipt_id, receipt_sha256=item.receipt_sha256)
            assert receipt.body_ref is not None
            assert read_body(conn, receipt.body_ref) in {
                FEED,
                FEED.replace(b"Summary", b"Corrected"),
            }
    skipped = articles.acquire_source(
        store,
        source="bbc_news",
        slot=SLOT + timedelta(hours=4),
        identity="repeat",
        directory=tmp_path / "skipped",
        session=Session([]),
    )
    assert skipped["status"] == "not_due"


def test_bbc_one_invalid_feed_does_not_discard_second_original(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    store = MemoryR2Store()
    publish_planning_input(store, "bbc_news", {})
    packet = tmp_path / "partial"
    report = articles.acquire_source(
        store,
        source="bbc_news",
        slot=SLOT,
        identity="partial",
        directory=packet,
        session=Session([Response(200, b"<html/>"), Response(200, FEED)]),
    )
    assert report["pages"] == 2
    assert report["status"] == "partial"
    assert "index_parse_failed" in report["failures"]
    imported = import_handoff("bbc_news", tmp_path / "market.sqlite", packet)
    assert not imported.complete
    assert len(imported.report["receipt_ids"]) == 2
    assert len(imported.report["observation_ids"]) == 1
