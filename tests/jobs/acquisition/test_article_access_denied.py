"""A source refusal ends this pass without discarding queued work or raw receipts."""

import json
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from tests.helpers.article_acquisition import SLOT, Response, Session
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.articles.store import connect_read_only
from baibai_jobs.acquisition import articles
from baibai_jobs.acquisition.article_planning import (
    load_planning,
    publish_planning_input,
    source_key,
)


@pytest.fixture(autouse=True)
def clock_and_wait(monkeypatch):
    class Clock(datetime):
        current = SLOT + timedelta(minutes=1)

        @classmethod
        def now(cls, _tz):
            return cls.current

    monkeypatch.setattr(articles, "datetime", Clock)
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    return Clock


def pending_store():
    store = MemoryR2Store()
    publish_planning_input(store, "minkabu_news", {})
    state, _ = load_planning(store, "minkabu_news")
    pending = ["https://minkabu.jp/news/100", "https://minkabu.jp/news/200"]
    state = replace(state, cursor={"pending_urls": pending, "pending_evidence": {}})
    store.store_object(source_key("minkabu_news"), state.encoded())
    return store, pending


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("denied_index", [False, True], ids=["document", "index"])
def test_refusal_stops_before_another_request_and_preserves_pending(tmp_path, status, denied_index):
    store, pending = pending_store()
    if denied_index:
        state, _ = load_planning(store, "minkabu_news")
        state = replace(
            state, cursor={**state.cursor, "next_page": "https://minkabu.jp/news?page=2"}
        )
        store.store_object(source_key("minkabu_news"), state.encoded())
    index = b'<a href="/news/100">Existing article</a>'
    session = Session(
        [Response(status, b"denied")]
        if denied_index
        else [Response(200, index), Response(status, b"denied")]
    )
    packet = tmp_path / "refused"
    result = articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT,
        identity="refused",
        directory=packet,
        session=session,
    )
    assert result["requests_used"] == (1 if denied_index else 2)
    assert session.urls == [articles.SOURCES["minkabu_news"].entry_url] + (
        [] if denied_index else pending[:1]
    )
    assert result["failures"] == ["http_error"]
    assert result["status"] == "partial"
    assert result["continuation_only"] is False
    state, _ = load_planning(store, "minkabu_news")
    assert state.cursor["pending_urls"] == pending
    assert state.cursor["next_page"] == ("https://minkabu.jp/news?page=2" if denied_index else None)
    assert state.not_before is None  # No fabricated cooldown or persistent source disable.
    with closing(connect_read_only(packet / "receipts.sqlite")) as conn:
        receipts = [
            json.loads(row[0])
            for row in conn.execute("SELECT receipt_json FROM article_source_receipts")
        ]
    refused = [receipt for receipt in receipts if receipt["http_status"] == status]
    assert len(refused) == 1
    assert refused[0]["status"] == "http_error"
    assert refused[0]["body_ref"] is None


def test_missing_document_does_not_end_other_independent_work(tmp_path):
    store, pending = pending_store()
    session = Session(
        [
            Response(200, b'<a href="/news/100">Existing article</a>'),
            Response(404, b"not found"),
            Response(200, b"<main>Another article</main>"),
        ]
    )
    result = articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT,
        identity="missing",
        directory=tmp_path / "missing",
        session=session,
    )
    assert session.urls == [articles.SOURCES["minkabu_news"].entry_url, *pending]
    assert result["requests_used"] == 3
    state, _ = load_planning(store, "minkabu_news")
    assert state.cursor["pending_urls"] == pending[:1]


@pytest.mark.parametrize("status", [401, 403])
def test_next_existing_slot_resumes_pending_after_refusal(tmp_path, clock_and_wait, status):
    store, pending = pending_store()
    articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT,
        identity="refused",
        directory=tmp_path / "refused",
        session=Session([Response(status, b"denied")]),
    )
    clock_and_wait.current += timedelta(hours=4)
    session = Session(
        [
            Response(200, b'<a href="/news/100">Existing article</a>'),
            Response(200, b"<main>Recovered first article</main>"),
            Response(200, b"<main>Recovered second article</main>"),
        ]
    )
    report = articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT + timedelta(hours=4),
        identity="recovered",
        directory=tmp_path / "recovered",
        session=session,
    )
    assert session.urls == [articles.SOURCES["minkabu_news"].entry_url, *pending]
    assert report["status"] == "completed"
    assert report["failures"] == []
    state, _ = load_planning(store, "minkabu_news")
    assert state.cursor["pending_urls"] == []
    with closing(connect_read_only(tmp_path / "refused/receipts.sqlite")) as conn:
        assert (
            json.loads(
                conn.execute("SELECT receipt_json FROM article_source_receipts").fetchone()[0]
            )["http_status"]
            == status
        )
