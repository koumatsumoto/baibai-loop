import json
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.observations.articles.store import read_observations
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition import articles
from baibai_jobs.acquisition.article_planning import load_planning, publish_planning_input
from baibai_jobs.acquisition.receipts import download_fixed
from baibai_jobs.integration.article_handoff import import_handoff

SLOT = datetime(2026, 10, 29, 13, 13, tzinfo=UTC)


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self.body = body
        self.headers = {"ETag": '"feed-v1"', "Content-Type": "application/xml"}
        self.encoding = "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, **_):
        yield self.body


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.headers = []
        self.urls = []

    def get(self, _url, **kwargs):
        assert kwargs["allow_redirects"] is False
        self.headers.append(kwargs["headers"])
        self.urls.append(_url)
        return next(self.responses)


def _nhk_description(text):
    document = {
        "@type": "NewsArticle",
        "headline": {"@value": "Synthetic JSON-LD headline", "@language": "ja"},
        "description": {"@value": text, "@language": "ja"},
        "mainEntityOfPage": {
            "@type": "WebPage",
            "@id": "https://news.web.nhk/news/example",
        },
    }
    return (
        b'<html><main>Synthetic page chrome</main><script type="application/ld+json">'
        + json.dumps(document, ensure_ascii=False).encode()
        + b"</script></html>"
    )


def test_second_slot_304_carries_unintegrated_200_closure(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, _tz):
            return SLOT + timedelta(hours=5)

    monkeypatch.setattr(articles, "datetime", Clock)
    store = MemoryR2Store()
    publish_planning_input(store, "boj_news", {"lake_release_id": "before"})
    session = Session([Response(200, b"<rss><channel /></rss>"), Response(304, b"")])
    for index in range(2):
        report = articles.acquire_source(
            store,
            source="boj_news",
            slot=SLOT + timedelta(hours=index * 4),
            identity=f"run-{index}",
            directory=tmp_path / f"slot-{index}",
            session=session,
        )
        assert report["status"] == "completed"
    assert "If-None-Match" not in session.headers[0]
    assert session.headers[1]["If-None-Match"] == '"feed-v1"'
    planning, _ = load_planning(store, "boj_news")
    assert len(planning.pending) == 2
    downloaded = tmp_path / "downloaded"
    download_fixed(store, planning.pending[-1], downloaded)
    with closing(connect_read_only(downloaded / "receipts.sqlite")) as conn:
        values = [
            json.loads(row[0])
            for row in conn.execute("SELECT receipt_json FROM article_source_receipts")
        ]
    assert sorted(item["status"] for item in values) == ["not_modified", "ok"]
    original = next(item for item in values if item["status"] == "ok")
    unchanged = next(item for item in values if item["status"] == "not_modified")
    assert unchanged["body_ref"] == original["body_ref"]
    assert (
        planning.requests[original["requested_url"]]["stage"]["stage_id"]
        == planning.pending[0]["stage_id"]
    )


def test_failure_in_one_source_does_not_stop_another_private_stage(tmp_path, monkeypatch, capsys):
    calls = []

    def acquire(_store, *, source, **_):
        calls.append(source)
        if source == "tsr_data":
            raise ValueError("synthetic private failure detail")
        return {"source": source, "status": "completed"}

    monkeypatch.setattr(articles, "acquire_source", acquire)
    monkeypatch.setattr(articles, "Boto3R2Store", lambda **_: MemoryR2Store())
    assert (
        articles.main(
            [
                "--group",
                "public",
                "--created-at",
                SLOT.isoformat(),
                "--identity",
                "test",
                "--directory",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert calls == list(articles.GROUPS["public"])
    assert "private failure detail" not in capsys.readouterr().out


def test_saved_tsr_body_limit_is_a_successful_finite_continuation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    source = "tsr_data"
    urls = [f"/data/detail/synthetic-{index}.html" for index in range(9)]
    index = json.dumps(
        [{"url": url, "title": f"Synthetic {index}"} for index, url in enumerate(urls)]
    )
    session = Session(
        [
            Response(200, index.encode()),
            *(Response(200, b"<div class='entry'>Synthetic body</div>") for _ in range(8)),
        ]
    )
    store = MemoryR2Store()
    publish_planning_input(store, source, {"lake_release_id": "before"})
    packet = tmp_path / "tsr"
    report = articles.acquire_source(
        store, source=source, slot=SLOT, identity="finite", directory=packet, session=session
    )
    assert report["status"] == "partial"
    assert report["continuation_only"] is True
    assert report["failures"] == []
    assert report["bodies"] == 8
    assert report["omitted_url_count"] == 0
    planning, _ = load_planning(store, source)
    assert len(planning.pending) == 1
    assert planning.cursor["pending_urls"] == [f"https://www.tsr-net.co.jp{urls[-1]}"]
    assert json.loads((packet / "result.json").read_text())["continuation_only"] is True

    def acquire(_store, *, source, **_kwargs):
        return report if source == "tsr_data" else {"source": source, "status": "not_due"}

    monkeypatch.setattr(articles, "acquire_source", acquire)
    monkeypatch.setattr(articles, "Boto3R2Store", lambda **_: MemoryR2Store())
    assert (
        articles.main(
            [
                "--group",
                "public",
                "--created-at",
                SLOT.isoformat(),
                "--identity",
                "finite",
                "--directory",
                str(tmp_path / "group"),
            ]
        )
        == 0
    )


def test_saved_next_page_alone_is_a_successful_finite_continuation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    pages = [
        Response(
            200,
            (
                '<a href="/news/1001">Synthetic headline</a>'
                f'<a rel="next" href="/news?page={page + 1}">次へ</a>'
            ).encode(),
        )
        for page in range(1, 4)
    ]
    store = MemoryR2Store()
    publish_planning_input(store, "minkabu_news", {"lake_release_id": "before"})
    report = articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT,
        identity="pages",
        directory=tmp_path / "pages",
        session=Session([*pages, Response(200, b"<main>Synthetic document</main>")]),
    )
    planning, _ = load_planning(store, "minkabu_news")
    assert report["status"] == "partial"
    assert report["continuation_only"] is True
    assert report["failures"] == []
    assert report["pages"] == 3
    assert planning.cursor["pending_urls"] == []
    assert planning.cursor["next_page"] is not None


def test_cursor_overflow_is_not_a_successful_continuation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    entries = [
        {"url": f"/data/detail/synthetic-{index}.html", "title": f"Synthetic {index}"}
        for index in range(articles.MAX_REQUEST_STATES + 9)
    ]
    session = Session(
        [
            Response(200, json.dumps(entries).encode()),
            *[Response(200, b"Synthetic body") for _ in range(8)],
        ]
    )
    store = MemoryR2Store()
    publish_planning_input(store, "tsr_data", {"lake_release_id": "before"})
    report = articles.acquire_source(
        store,
        source="tsr_data",
        slot=SLOT,
        identity="overflow",
        directory=tmp_path / "overflow",
        session=session,
    )
    planning, _ = load_planning(store, "tsr_data")
    assert report["status"] == "partial"
    assert report["continuation_only"] is False
    assert "pending_cursor_overflow" in report["failures"]
    assert report["omitted_url_count"] == 1
    assert len(planning.cursor["pending_urls"]) == articles.MAX_REQUEST_STATES
    monkeypatch.setattr(
        articles,
        "acquire_source",
        lambda _store, *, source, **_kwargs: (
            report if source == "tsr_data" else {"source": source, "status": "not_due"}
        ),
    )
    monkeypatch.setattr(articles, "Boto3R2Store", lambda **_: MemoryR2Store())
    assert (
        articles.main(
            [
                "--group",
                "public",
                "--created-at",
                SLOT.isoformat(),
                "--identity",
                "overflow",
                "--directory",
                str(tmp_path / "group"),
            ]
        )
        == 1
    )


def test_nhk_four_feeds_keep_each_summary_and_fetch_shared_guid_once(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, _tz):
            return SLOT + timedelta(minutes=1)

    monkeypatch.setattr(articles, "datetime", Clock)
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    store = MemoryR2Store()
    publish_planning_input(store, "nhk_news", {"lake_release_id": "before"})
    feeds = [
        Response(
            200,
            f"<rss><channel><item><guid>synthetic-same-guid</guid><link>https://news.web.nhk/news/example</link><title>Synthetic title</title><description>synthetic summary {index}</description></item></channel></rss>".encode(),
        )
        for index in range(4)
    ]
    session = Session(
        [*feeds, Response(200, b"<html><body>Unsupported synthetic shell</body></html>")]
    )
    report = articles.acquire_source(
        store,
        source="nhk_news",
        slot=SLOT,
        identity="test",
        directory=tmp_path / "nhk",
        session=session,
    )
    assert report["pages"] == 4
    assert report["bodies"] == 1
    assert len(set(session.urls)) == 5
    with closing(connect_read_only(tmp_path / "nhk/receipts.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM article_source_receipts").fetchone()[0] == 5
    planning, _ = load_planning(store, "nhk_news")
    assert len(planning.pending) == 1
    projected = import_handoff("nhk_news", tmp_path / "market.sqlite", tmp_path / "nhk")
    assert not projected.complete
    saved = json.loads((tmp_path / "nhk/result.json").read_text())
    document_id = next(
        identity for identity, kind in saved["projection_kinds"].items() if kind == "document"
    )
    assert document_id in projected.report["projection_failures"]


@pytest.mark.parametrize("damaged", ["missing", "corrupt"])
def test_missing_old_200_closure_uses_one_unconditional_get_instead_of_repeated_304(
    tmp_path, monkeypatch, damaged
):
    class Clock(datetime):
        @classmethod
        def now(cls, _tz):
            return SLOT + timedelta(hours=9)

    monkeypatch.setattr(articles, "datetime", Clock)
    store = MemoryR2Store()
    publish_planning_input(store, "boj_news", {"lake_release_id": "before"})
    session = Session(
        [
            Response(200, b"<rss><channel /></rss>"),
            Response(200, b"<rss><channel /></rss>"),
            Response(304, b""),
        ]
    )
    first = articles.acquire_source(
        store,
        source="boj_news",
        slot=SLOT,
        identity="first",
        directory=tmp_path / "first",
        session=session,
    )
    planning, _ = load_planning(store, "boj_news")
    stage_id = planning.requests[articles.SOURCES["boj_news"].entry_url]["stage"]["stage_id"]
    key = f"coordination/article-sources/{stage_id}/handoff.json"
    if damaged == "missing":
        store.values.pop(key)
    else:
        store.store_object(key, b"corrupt")
    second = articles.acquire_source(
        store,
        source="boj_news",
        slot=SLOT + timedelta(hours=4),
        identity="second",
        directory=tmp_path / "second",
        session=session,
    )
    third = articles.acquire_source(
        store,
        source="boj_news",
        slot=SLOT + timedelta(hours=8),
        identity="third",
        directory=tmp_path / "third",
        session=session,
    )
    assert first["status"] == "completed"
    assert second["status"] == "partial"
    assert "baseline_unavailable" in second["failures"]
    assert third["status"] == "completed"
    assert "If-None-Match" not in session.headers[1]
    assert session.headers[2]["If-None-Match"] == '"feed-v1"'


@pytest.mark.parametrize(("status", "expected_pages"), [(403, 4), (429, 1), (200, 4)])
def test_nhk_independent_feed_failure_continues_except_cooldown(
    tmp_path, monkeypatch, status, expected_pages
):
    class Clock(datetime):
        @classmethod
        def now(cls, _tz):
            return SLOT + timedelta(minutes=1)

    monkeypatch.setattr(articles, "datetime", Clock)
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    store = MemoryR2Store()
    publish_planning_input(store, "nhk_news", {"lake_release_id": "before"})
    responses = [Response(status, b"<invalid>" if status == 200 else b"")]
    if status != 429:
        responses.extend(Response(200, b"<rss><channel /></rss>") for _ in range(3))
    session = Session(responses)
    report = articles.acquire_source(
        store,
        source="nhk_news",
        slot=SLOT,
        identity="failure",
        directory=tmp_path / "nhk",
        session=session,
    )
    assert report["status"] == "partial"
    assert report["continuation_only"] is False
    if status == 200:
        assert "index_parse_failed" in report["failures"]
    assert report["pages"] == expected_pages
    assert len(session.urls) == expected_pages
    planning, _ = load_planning(store, "nhk_news")
    assert len(planning.pending) == 1
    with closing(connect_read_only(tmp_path / "nhk/receipts.sqlite")) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM article_source_receipts").fetchone()[0]
            == expected_pages
        )


def test_nhk_pending_body_keeps_exact_guid_when_item_leaves_feed(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, _tz):
            return SLOT + timedelta(hours=5)

    monkeypatch.setattr(articles, "datetime", Clock)
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    feed = (
        b"<rss><channel><item><guid>synthetic-guid</guid>"
        b"<link>https://news.web.nhk/news/example</link>"
        b"<title>Synthetic original title</title></item></channel></rss>"
    )
    empty = b"<rss><channel /></rss>"
    body = _nhk_description("Synthetic retained summary")
    session = Session(
        [
            Response(200, feed),
            *(Response(200, empty) for _ in range(3)),
            Response(403, b""),
            Response(200, empty),
            *(Response(304, b"") for _ in range(3)),
            Response(200, body),
        ]
    )
    store = MemoryR2Store()
    publish_planning_input(store, "nhk_news", {"lake_release_id": "before"})
    first = tmp_path / "first"
    first_report = articles.acquire_source(
        store,
        source="nhk_news",
        slot=SLOT,
        identity="first",
        directory=first,
        session=session,
    )
    assert first_report["status"] == "partial"
    planning, _ = load_planning(store, "nhk_news")
    url = "https://news.web.nhk/news/example"
    original = planning.cursor["pending_evidence"][url]
    assert original["stage"]["manifest_sha256"] == first_report["manifest_sha256"]
    second = tmp_path / "second"
    second_report = articles.acquire_source(
        store,
        source="nhk_news",
        slot=SLOT + timedelta(hours=4),
        identity="second",
        directory=second,
        session=session,
    )
    assert second_report["status"] == "completed"
    saved = json.loads((second / "result.json").read_text())
    assert original["index_receipt_id"] in saved["receipt_ids"]
    assert (
        next(iter(saved["document_evidence"].values()))["index_receipt_id"]
        == original["index_receipt_id"]
    )
    database = tmp_path / "market.sqlite"
    result = import_handoff("nhk_news", database, second)
    with closing(open_connection(database)) as conn:
        observations = read_observations(conn, result.report["observation_ids"])
    document = next(
        item for item in observations if item.native_locator == "/html/newsarticle-description"
    )
    assert document.article_id == "nhk_news:synthetic-guid"
    assert document.title == "Synthetic original title"
    assert document.text == "Synthetic retained summary"
    assert document.content_scope == "summary"
    assert document.missing_parts == ("full_article_body",)
    assert document.native_metadata["description_language"] == "ja"
    assert document.native_metadata["index_observation_id"] in {
        item.observation_id
        for item in observations
        if item.receipt_id == original["index_receipt_id"]
    }


def test_nhk_guid_evidence_survives_document_and_later_304(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, _tz):
            return SLOT + timedelta(hours=5)

    monkeypatch.setattr(articles, "datetime", Clock)
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    feed = (
        b"<rss><channel><item><guid>synthetic-guid</guid>"
        b"<link>https://news.web.nhk/news/example</link>"
        b"<title>Synthetic index title</title>"
        b"<description>Synthetic summary</description></item></channel></rss>"
    )
    empty = b"<rss><channel /></rss>"
    body = _nhk_description("Synthetic article summary")
    session = Session(
        [
            Response(200, feed),
            *(Response(200, empty) for _ in range(3)),
            Response(200, body),
            *(Response(304, b"") for _ in range(5)),
        ]
    )
    store = MemoryR2Store()
    publish_planning_input(store, "nhk_news", {"lake_release_id": "before"})
    database = tmp_path / "market.sqlite"
    documents = []
    for index in range(2):
        packet = tmp_path / f"slot-{index}"
        report = articles.acquire_source(
            store,
            source="nhk_news",
            slot=SLOT + timedelta(hours=4 * index),
            identity=f"run-{index}",
            directory=packet,
            session=session,
        )
        assert report["bodies"] == 1
        result = import_handoff("nhk_news", database, packet)
        with closing(open_connection(database)) as conn:
            observations = read_observations(conn, result.report["observation_ids"])
        document = next(
            item for item in observations if item.native_locator == "/html/newsarticle-description"
        )
        documents.append(document)
    assert all(item.article_id == "nhk_news:synthetic-guid" for item in documents)
    assert all(item.title == "Synthetic index title" for item in documents)
    assert all(item.content_scope == "summary" for item in documents)
    assert all(item.missing_parts == ("full_article_body",) for item in documents)
    assert all("index_observation_id" in item.native_metadata for item in documents)
    assert documents[0].article_version_id == documents[1].article_version_id


def test_tsr_index_observation_binds_document_title_without_heading(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, _tz):
            return SLOT + timedelta(minutes=1)

    monkeypatch.setattr(articles, "datetime", Clock)
    index = json.dumps(
        [{"url": "/data/detail/synthetic.html", "title": "Synthetic TSR index title"}]
    ).encode()
    body = b'<html><body><div class="entry">Synthetic TSR body</div></body></html>'
    session = Session([Response(200, index), Response(200, body)])
    store = MemoryR2Store()
    publish_planning_input(store, "tsr_data", {"lake_release_id": "before"})
    packet = tmp_path / "tsr"
    articles.acquire_source(
        store,
        source="tsr_data",
        slot=SLOT,
        identity="test",
        directory=packet,
        session=session,
    )
    database = tmp_path / "market.sqlite"
    result = import_handoff("tsr_data", database, packet)
    with closing(open_connection(database)) as conn:
        observations = read_observations(conn, result.report["observation_ids"])
    document = next(item for item in observations if item.native_locator == "/html/body-region")
    assert document.title == "Synthetic TSR index title"
    assert document.article_id == "tsr_data:https://www.tsr-net.co.jp/data/detail/synthetic.html"
    assert document.native_metadata["index_observation_id"] in {
        item.observation_id for item in observations if item.native_locator == "/0"
    }


def test_nhk_conflicting_guids_for_one_url_keep_raw_without_guessed_projection(
    tmp_path, monkeypatch
):
    class Clock(datetime):
        @classmethod
        def now(cls, _tz):
            return SLOT + timedelta(minutes=1)

    monkeypatch.setattr(articles, "datetime", Clock)
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )

    def feed(guid):
        return (
            "<rss><channel><item>"
            f"<guid>{guid}</guid><link>https://news.web.nhk/news/example</link>"
            "<title>Synthetic title</title></item></channel></rss>"
        ).encode()

    session = Session(
        [
            Response(200, feed("guid-a")),
            Response(200, feed("guid-b")),
            *(Response(200, b"<rss><channel /></rss>") for _ in range(2)),
            Response(200, _nhk_description("Synthetic disputed summary")),
        ]
    )
    store = MemoryR2Store()
    publish_planning_input(store, "nhk_news", {"lake_release_id": "before"})
    packet = tmp_path / "nhk"
    report = articles.acquire_source(
        store,
        source="nhk_news",
        slot=SLOT,
        identity="ambiguous",
        directory=packet,
        session=session,
    )
    assert report["status"] == "partial"
    assert "document_identity_unbound" in report["failures"]
    assert json.loads((packet / "result.json").read_text())["document_evidence"] == {}
    result = import_handoff("nhk_news", tmp_path / "market.sqlite", packet)
    assert not result.complete
    assert result.report["projection_failures"]
    with closing(connect_read_only(tmp_path / "market.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM article_source_receipts").fetchone()[0] == 5
