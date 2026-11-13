"""A source refusal ends this pass without discarding queued work or raw receipts."""

import json
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from tests.helpers.article_acquisition import SLOT, Response, Session
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.data_collection.receipts.articles.store import connect_read_only
from baibai_engine.storage.objects.json import read_private
from baibai_jobs.acquisition import article_planning, articles
from baibai_jobs.acquisition.article_planning import (
    load_planning,
    publish_planning_input,
    source_key,
)
from baibai_jobs.acquisition.economic_staging import stage_prefix


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


def test_failed_documents_yield_next_slot_to_unattempted_articles(
    tmp_path, monkeypatch, clock_and_wait
):
    source = "toushiru"
    policy = replace(articles.SOURCES[source], max_bodies=2)
    monkeypatch.setitem(articles.SOURCES, source, policy)
    urls = [f"https://media.rakuten-sec.net/articles/-/{number}" for number in (1, 2, 3)]
    index = (
        "<urlset>"
        + "".join(f"<url><loc>{url}</loc><title>Synthetic</title></url>" for url in urls)
        + "</urlset>"
    ).encode()
    store = MemoryR2Store()
    publish_planning_input(store, source, {})
    first = Session([Response(200, index), Response(404, b""), Response(404, b"")])
    report = articles.acquire_source(
        store,
        source=source,
        slot=SLOT,
        identity="missing-documents",
        directory=tmp_path / "first",
        session=first,
    )
    assert first.urls == [policy.entry_url, *urls[:2]]
    assert report["failures"] == ["http_error"]
    state, _ = load_planning(store, source)
    assert state.cursor["pending_urls"] == [urls[2], *urls[:2]]

    clock_and_wait.current += timedelta(hours=4)
    second = Session(
        [Response(200, index), Response(200, b"<main>New article</main>"), Response(404, b"")]
    )
    articles.acquire_source(
        store,
        source=source,
        slot=SLOT + timedelta(hours=4),
        identity="advance-unattempted",
        directory=tmp_path / "second",
        session=second,
    )
    assert second.urls == [policy.entry_url, urls[2], urls[0]]
    state, _ = load_planning(store, source)
    assert state.cursor["pending_urls"] == [urls[1], urls[0]]
    with closing(connect_read_only(tmp_path / "first/receipts.sqlite")) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM article_source_receipts "
                "WHERE json_extract(receipt_json, '$.http_status')=404"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("status", [429, 503])
def test_provider_cooldown_preserves_the_blocked_document_first(tmp_path, status):
    store, pending = pending_store()
    session = Session(
        [Response(200, b'<a href="/news/100">Existing article</a>'), Response(status, b"")]
    )
    result = articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT,
        identity="cooldown",
        directory=tmp_path / "cooldown",
        session=session,
    )
    assert result["requests_used"] == 2
    state, _ = load_planning(store, "minkabu_news")
    assert state.cursor["pending_urls"] == pending
    assert state.not_before is not None


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


@pytest.mark.parametrize("status", [401, 403])
def test_refused_resume_page_keeps_exact_checkpoint_for_next_slot(tmp_path, clock_and_wait, status):
    store, pending = pending_store()
    state, _ = load_planning(store, "minkabu_news")
    resume = "https://minkabu.jp/news?page=10"
    state = replace(state, cursor={**state.cursor, "next_page": resume})
    store.store_object(source_key("minkabu_news"), state.encoded())
    root = b'<a href="/news/100">Existing article</a><a rel="next" href="/news?page=2">Next</a>'
    first = Session([Response(200, root), Response(status, b"denied")])
    articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT,
        identity="refused-page",
        directory=tmp_path / "refused-page",
        session=first,
    )
    assert first.urls == [articles.SOURCES["minkabu_news"].entry_url, resume]
    state, _ = load_planning(store, "minkabu_news")
    assert state.cursor["next_page"] == resume
    assert state.cursor["pending_urls"] == pending

    clock_and_wait.current += timedelta(hours=4)
    following = Session(
        [
            Response(200, root),
            Response(200, b'<a href="/news/100">Existing article</a>'),
            *[Response(200, b"<main>Recovered article</main>") for _ in pending],
        ]
    )
    result = articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT + timedelta(hours=4),
        identity="resumed-page",
        directory=tmp_path / "resumed-page",
        session=following,
    )
    assert following.urls == [articles.SOURCES["minkabu_news"].entry_url, resume, *pending]
    assert result["status"] == "completed"


@pytest.mark.parametrize("status", [401, 403])
def test_refusal_preserves_work_beyond_validator_cache_bound(tmp_path, clock_and_wait, status):
    store, _ = pending_store()
    old = [f"https://minkabu.jp/news/{n}" for n in range(100, 227)]
    fresh = [f"https://minkabu.jp/news/{n}" for n in range(300, 304)]
    state, _ = load_planning(store, "minkabu_news")
    state = replace(state, cursor={"pending_urls": old, "pending_evidence": {}})
    store.store_object(source_key("minkabu_news"), state.encoded())
    index = "".join(f'<a href="{url}">Fresh article</a>' for url in fresh).encode()
    session = Session([Response(200, index), Response(status, b"denied")])
    report = articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT,
        identity="large-refused",
        directory=tmp_path / "large-refused",
        session=session,
    )
    expected = [*old, *fresh]
    state, _ = load_planning(store, "minkabu_news")
    assert session.urls == [articles.SOURCES["minkabu_news"].entry_url, old[0]]
    assert report["pending_url_count"] == len(expected) == 131
    assert report["omitted_url_count"] == 0
    assert state.cursor["pending_urls"] == expected
    assert set(fresh) <= state.cursor["pending_evidence"].keys()

    clock_and_wait.current += timedelta(hours=4)
    resumed = Session([Response(200, index), Response(status, b"denied")])
    report = articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT + timedelta(hours=4),
        identity="large-resumed",
        directory=tmp_path / "large-resumed",
        session=resumed,
    )
    state, _ = load_planning(store, "minkabu_news")
    assert resumed.urls == [articles.SOURCES["minkabu_news"].entry_url, old[0]]
    assert report["pending_url_count"] == len(expected)
    assert report["omitted_url_count"] == 0
    assert state.cursor["pending_urls"] == expected


def test_planning_byte_limit_retains_full_work_in_immutable_stage(tmp_path, monkeypatch):
    store, _ = pending_store()
    old = [f"https://minkabu.jp/news/{n}" for n in range(100, 227)]
    fresh = [f"https://minkabu.jp/news/{n}" for n in range(300, 304)]
    state, _ = load_planning(store, "minkabu_news")
    state = replace(state, cursor={"pending_urls": old, "pending_evidence": {}})
    store.store_object(source_key("minkabu_news"), state.encoded())
    monkeypatch.setattr(article_planning, "MAX_PLANNING_BYTES", len(state.encoded()) + 2048)
    index = "".join(f'<a href="{url}">{"Long title " * 500}</a>' for url in fresh).encode()
    packet = tmp_path / "capacity"
    session = Session([Response(200, index), Response(403, b"denied")])
    with pytest.raises(ValueError, match="planning summary exceeds"):
        articles.acquire_source(
            store,
            source="minkabu_news",
            slot=SLOT,
            identity="capacity",
            directory=packet,
            session=session,
        )
    assert session.urls == [articles.SOURCES["minkabu_news"].entry_url, old[0]]
    entry = json.loads((packet / "stage.json").read_text())
    manifest = read_private(
        store,
        stage_prefix(entry["stage_id"], namespace=article_planning.ARTICLE_NAMESPACE)
        + "/handoff.json",
    )
    assert manifest is not None
    cursor = json.loads(manifest.payload)["result"]["planning"]["cursor"]
    assert cursor["pending_urls"] == [*old, *fresh]
    assert set(fresh) <= cursor["pending_evidence"].keys()
    assert cursor["omitted_url_count"] == 0
    unchanged, _ = load_planning(store, "minkabu_news")
    assert unchanged.cursor["pending_urls"] == old
    assert unchanged.last_stage is None


def test_refusal_does_not_erase_known_historical_omissions(tmp_path):
    store, _ = pending_store()
    state, _ = load_planning(store, "minkabu_news")
    state = replace(state, cursor={**state.cursor, "omitted_url_count": 3})
    store.store_object(source_key("minkabu_news"), state.encoded())
    report = articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT,
        identity="old-omission",
        directory=tmp_path / "old-omission",
        session=Session([Response(403, b"denied")]),
    )
    state, _ = load_planning(store, "minkabu_news")
    assert report["omitted_url_count"] == state.cursor["omitted_url_count"] == 3
    assert "legacy_omission_unresolved" in report["failures"]
