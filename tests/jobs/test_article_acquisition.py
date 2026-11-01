import json
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.r2_store import MemoryR2Store

from baibai_engine.machine_analysis.observations.articles.store import read_observations
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import open_connection
from baibai_jobs.acquisition import articles
from baibai_jobs.acquisition.article_planning import (
    load_discovery_cursor,
    load_frontier_cursor,
    load_planning,
    publish_planning_input,
)
from baibai_jobs.acquisition.cooldown import extend, quota_key
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


def test_external_assessment_quota_wait_blocks_article_before_http(tmp_path):
    store = MemoryR2Store()
    publish_planning_input(store, "minkabu_news", {"lake_release_id": "before"})
    extend(store, quota_key("minkabu"), datetime.now(UTC) + timedelta(hours=1))
    session = Session([])
    packet = tmp_path / "waiting"
    report = articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT,
        identity="next-work",
        directory=packet,
        session=session,
    )
    assert report["reason"] == "retry_not_before"
    assert session.urls == []
    assert not packet.exists()


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


@pytest.mark.parametrize("target_status", [200, 403])
@pytest.mark.parametrize("bureau", [False, True])
def test_mof_inside_link_retains_notice_and_target_as_separate_receipts(
    tmp_path, monkeypatch, target_status, bureau
):
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    notice_url = "https://www.mof.go.jp/insideLink/synthetic.html"
    target_url = (
        "https://lfb.mof.go.jp/renkei/torikumi.html"
        if bureau
        else "https://www.mof.go.jp/pri/research/seminar/lmeeting.htm"
    )
    feed = (
        f"<rss><channel><item><title>Synthetic</title><link>{notice_url}</link>"
        "</item></channel></rss>"
    ).encode()
    notice = f'<meta http-equiv="refresh" content="0;url={target_url}">'.encode()
    target_body = (
        '<meta http-equiv="Content-Type" content="text/html; charset=shift_jis">'
        "<main>本文" + ("" if bureau else '<a href="old.pdf">Old reference</a>') + "</main>"
    ).encode("shift_jis")
    session = Session(
        [
            Response(200, feed),
            Response(200, notice),
            Response(target_status, target_body),
        ]
    )
    store = MemoryR2Store()
    publish_planning_input(store, "mof_news", {"lake_release_id": "before"})
    packet = tmp_path / "mof"
    report = articles.acquire_source(
        store, source="mof_news", slot=SLOT, identity="notice", directory=packet, session=session
    )
    assert session.urls == [articles.SOURCES["mof_news"].entry_url, notice_url, target_url]
    assert report["bodies"] == 2
    assert report["requests_used"] == 3
    with closing(connect_read_only(packet / "receipts.sqlite")) as conn:
        receipts = [
            json.loads(row[0])
            for row in conn.execute("SELECT receipt_json FROM article_source_receipts")
        ]
    assert len(receipts) == 3
    assert {item["requested_url"] for item in receipts} == set(session.urls)
    database = tmp_path / "canonical.sqlite"
    result = import_handoff("mof_news", database, packet)
    with closing(connect_read_only(database)) as conn:
        observations = read_observations(conn, result.report["observation_ids"])
    notice_observation = next(
        item for item in observations if item.native_locator == "/html/meta-refresh"
    )
    assert notice_observation.content_scope == "unavailable"
    assert notice_observation.text == ""
    assert notice_observation.native_metadata["redirect_target_url"] == target_url
    if target_status == 200:
        assert report["status"] == "completed"
        assert report["failures"] == []
        assert result.complete is True
        target = next(item for item in observations if item.original_url == target_url)
        assert "本文" in target.text
        target_receipt = next(item for item in receipts if item["requested_url"] == target_url)
        assert target_receipt["encoding"] == "shift_jis"
        if not bureau:
            assert target.native_metadata["attachment_scope"] == "rolling_listing_reference_only"
        assert target.receipt_id != notice_observation.receipt_id
        assert target.article_id != notice_observation.article_id
    else:
        assert report["status"] == "partial"
        assert report["continuation_only"] is False
        assert report["failures"] == ["http_error"]
        assert result.complete is False
        assert all(item.original_url != target_url for item in observations)


def test_mof_csv_does_not_abort_later_documents_and_keeps_original_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    csv_url = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
    html_url = "https://www.mof.go.jp/synthetic.html"
    feed = (
        f"<rss><channel><item><title>CSV</title><link>{csv_url}</link></item>"
        f"<item><title>HTML</title><link>{html_url}</link></item></channel></rss>"
    ).encode()
    csv_text = "合成金利表,,\r\n基準日,1年,2年\r\nR8.1.5,0.5,0.6\r\n"
    csv_response = Response(200, csv_text.encode("cp932"))
    csv_response.headers["Content-Type"] = "text/csv"
    session = Session(
        [Response(200, feed), csv_response, Response(200, b"<main>Later document</main>")]
    )
    store = MemoryR2Store()
    publish_planning_input(store, "mof_news", {})
    packet = tmp_path / "mof-csv"
    report = articles.acquire_source(
        store, source="mof_news", slot=SLOT, identity="csv", directory=packet, session=session
    )
    assert session.urls == [articles.SOURCES["mof_news"].entry_url, csv_url, html_url]
    assert report["status"] == "completed"
    assert report["failures"] == []
    assert report["bodies"] == 2
    with closing(connect_read_only(packet / "receipts.sqlite")) as conn:
        receipts = [
            json.loads(row[0])
            for row in conn.execute("SELECT receipt_json FROM article_source_receipts")
        ]
    original = next(item for item in receipts if item["requested_url"] == csv_url)
    assert original["encoding"] == "utf-8"  # The original transport fallback is not rewritten.
    assert original["media_type"] == "text/csv"
    database = tmp_path / "canonical.sqlite"
    result = import_handoff("mof_news", database, packet)
    assert result.complete is True
    with closing(connect_read_only(database)) as conn:
        observations = read_observations(conn, result.report["observation_ids"])
    csv_observation = next(
        item for item in observations if item.receipt_id == original["receipt_id"]
    )
    assert csv_observation.original_url == csv_url
    assert csv_observation.native_locator == "/csv"
    assert csv_observation.text == csv_text
    assert csv_observation.native_metadata["encoding"] == "cp932"
    assert any(
        item.original_url == html_url and item.text == "Later document" for item in observations
    )


def test_mof_redirect_target_remains_pending_when_body_budget_is_spent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    monkeypatch.setitem(
        articles.SOURCES, "mof_news", replace(articles.SOURCES["mof_news"], max_bodies=1)
    )
    notice_url = "https://www.mof.go.jp/insideLink/synthetic.html"
    target_url = "https://www.mof.go.jp/pri/research/synthetic.htm"
    feed = f"<rss><channel><item><link>{notice_url}</link></item></channel></rss>".encode()
    notice = f'<meta http-equiv="refresh" content="0;url={target_url}">'.encode()
    session = Session([Response(200, feed), Response(200, notice)])
    store = MemoryR2Store()
    publish_planning_input(store, "mof_news", {"lake_release_id": "before"})
    report = articles.acquire_source(
        store,
        source="mof_news",
        slot=SLOT,
        identity="bounded-notice",
        directory=tmp_path / "bounded",
        session=session,
    )
    assert report["status"] == "partial"
    assert report["continuation_only"] is True
    assert report["failures"] == []
    assert report["bodies"] == 1
    assert len(session.urls) == 2
    state, _ = load_planning(store, "mof_news")
    assert load_discovery_cursor(store, state)["pending_urls"] == [target_url]


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
    frontier = load_frontier_cursor(store, planning)
    assert [item[0] for item in frontier["pending"]] == [f"https://www.tsr-net.co.jp{urls[-1]}"]
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


def test_self_linked_next_page_retains_index_but_is_not_successful_continuation(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    store = MemoryR2Store()
    publish_planning_input(store, "minkabu_news", {"lake_release_id": "before"})
    packet = tmp_path / "self-link"
    report = articles.acquire_source(
        store,
        source="minkabu_news",
        slot=SLOT,
        identity="self-link",
        directory=packet,
        session=Session(
            [
                Response(
                    200,
                    b'<a href="/news/1001">Synthetic headline</a>'
                    b'<a rel="next" href="">Next page</a>',
                ),
                Response(200, b"<main>Synthetic document</main>"),
            ]
        ),
    )
    assert report["status"] == "partial"
    assert report["continuation_only"] is False
    assert report["failures"] == ["index_page_cycle"]
    assert report["pages"] == 1
    planning, _ = load_planning(store, "minkabu_news")
    assert planning.cursor["next_page"] == articles.SOURCES["minkabu_news"].entry_url
    assert len(planning.pending) == 1
    with closing(connect_read_only(packet / "receipts.sqlite")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM article_source_receipts").fetchone()[0] == 2


def test_large_index_retains_all_pending_work_outside_the_mutable_summary(tmp_path, monkeypatch):
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
    assert report["continuation_only"] is True
    assert report["failures"] == []
    assert report["omitted_url_count"] == 0
    frontier = load_frontier_cursor(store, planning)
    assert len(frontier["pending"]) == articles.MAX_REQUEST_STATES + 1
    assert set(planning.cursor) == {"format", "stage"}
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
        == 0
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
    # Keep the workset independently intact: only the evictable HTTP validator's
    # old baseline is damaged below. Losing pending work must fail before GET.
    from tests.jobs.test_article_planning import _packet

    from baibai_jobs.acquisition.article_planning import commit_stage

    private = tmp_path / "independent-workset"
    private.mkdir()
    current, etag = load_planning(store, "boj_news")
    commit_stage(
        store,
        state=current,
        expected_etag=etag,
        slot=SLOT,
        stage_id="intact-workset",
        directory=_packet(private),
        requests=current.requests,
        cursor=load_discovery_cursor(store, current),
        not_before=current.not_before,
        advance_slot=False,
    )
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
    frontier = load_frontier_cursor(store, planning)
    pending = next(refs for pending_url, refs in frontier["pending"] if pending_url == url)
    original = frontier["indexes"][pending[0][0]]
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


@pytest.mark.parametrize(("hour", "manual"), [(13, False), (1, True)])
def test_tsr_index_observation_binds_document_title_without_heading(
    tmp_path, monkeypatch, hour, manual
):
    slot = SLOT.replace(hour=hour)

    class Clock(datetime):
        @classmethod
        def now(cls, _tz):
            return slot + timedelta(minutes=1)

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
        slot=slot,
        identity="test",
        directory=packet,
        session=session,
        manual=manual,
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


def test_native_tsr_multislot_frontier_reaches_urls_evicted_from_validator_cache(
    tmp_path, monkeypatch
):
    from baibai_jobs.acquisition.article_planning import acknowledge_stage

    monkeypatch.setattr(
        "baibai_engine.data_collection.transport.articles.wait_for_read_retry", lambda _: None
    )
    source = "tsr_data"
    rows = [{"url": f"/data/detail/native-{i}.html", "title": f"Synthetic {i}"} for i in range(137)]
    feed = json.dumps(rows).encode()
    wanted = {"https://www.tsr-net.co.jp" + row["url"] for row in rows}
    observed = set()

    class NativeSession:
        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            if url == articles.SOURCES[source].entry_url:
                return Response(200, feed)
            assert url in wanted
            observed.add(url)
            return Response(200, b"<div class='entry'>Synthetic body</div>")

    class Clock(datetime):
        instant = SLOT

        @classmethod
        def now(cls, _tz):
            return cls.instant

    monkeypatch.setattr(articles, "datetime", Clock)
    monkeypatch.setattr("baibai_engine.data_collection.transport.articles.datetime", Clock)
    store = MemoryR2Store()
    publish_planning_input(store, source, {})
    for number in range(24):
        Clock.instant = SLOT + timedelta(days=number)
        report = articles.acquire_source(
            store,
            source=source,
            slot=Clock.instant,
            identity=str(number),
            directory=tmp_path / f"slot-{number}",
            session=NativeSession(),
        )
        assert report["bodies"] <= 8
        assert report["failures"] == []
        state, _ = load_planning(store, source)
        assert len(state.requests) <= 128
        acknowledge_stage(store, state.pending[-1])
    assert observed == wanted
    state, _ = load_planning(store, source)
    assert load_frontier_cursor(store, state)["pending"] == []


def test_partial_discovery_before_body_queue_still_saves_all_discovered_work(tmp_path, monkeypatch):
    from baibai_engine.data_collection.transport.articles import ArticleBudgetExceeded

    feed = (
        b"<rss><channel><item><guid>one</guid>"
        b"<link>https://news.web.nhk/news/one</link><title>One</title>"
        b"</item></channel></rss>"
    )
    session = Session([Response(200, feed)])
    original = articles.fetch_article
    calls = 0

    def stop_after_first(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ArticleBudgetExceeded("synthetic deadline")
        return original(*args, **kwargs)

    monkeypatch.setattr(articles, "fetch_article", stop_after_first)
    store = MemoryR2Store()
    publish_planning_input(store, "nhk_news", {})
    report = articles.acquire_source(
        store,
        source="nhk_news",
        slot=SLOT,
        identity="partial-discovery",
        directory=tmp_path / "partial-discovery",
        session=session,
    )
    assert report["bodies"] == 0
    assert report["continuation_only"] is False
    assert "ArticleBudgetExceeded" in report["failures"]
    state, _ = load_planning(store, "nhk_news")
    frontier = load_frontier_cursor(store, state)
    assert [url for url, _ in frontier["pending"]] == ["https://news.web.nhk/news/one"]
    assert len(frontier["observed"]) == 1


@pytest.mark.parametrize("manual", [False, True])
def test_article_cli_passes_manual_selection(tmp_path, monkeypatch, manual):
    calls = []

    def acquire(_store, **kwargs):
        calls.append(kwargs)
        return {"source": kwargs["source"], "status": "not_due"}

    monkeypatch.setattr(articles, "Boto3R2Store", lambda **_: MemoryR2Store())
    monkeypatch.setattr(articles, "acquire_source", acquire)
    args = [
        "--group",
        "public",
        "--created-at",
        SLOT.isoformat(),
        "--identity",
        "test",
        "--directory",
        str(tmp_path),
    ]
    if manual:
        args.append("--manual")
    assert articles.main(args) == 0
    assert {call["source"] for call in calls} == set(articles.GROUPS["public"])
    assert all(call["manual"] is manual and call["slot"] == SLOT for call in calls)
