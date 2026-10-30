"""Finite synthetic provider/transport cases; no public originals or network calls."""

import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
import requests

from baibai_engine.data_collection.providers.articles.contracts import SOURCES
from baibai_engine.data_collection.providers.articles.native import (
    next_index_url,
    parse_document,
    parse_index,
)
from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt, BodyRef
from baibai_engine.data_collection.transport.articles import (
    ArticleBudget,
    ArticleBudgetExceeded,
    fetch_article,
)
from baibai_engine.machine_analysis.observations.articles.projection import native_time


def response(status, body=b"", **headers):
    result = Mock(spec=requests.Response)
    result.status_code = status
    result.headers = headers
    result.encoding = "utf-8"
    result.iter_content.return_value = iter([body])
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=None)
    return result


def fetch(session, *, baseline=None, budget=None):
    return fetch_article(
        session,
        source="tsr_data",
        url=SOURCES["tsr_data"].entry_url,
        allowed_hosts=SOURCES["tsr_data"].allowed_hosts,
        budget=budget or ArticleBudget.start(max_requests=3, max_bytes=1000, seconds=30),
        max_response_bytes=500,
        baseline=baseline,
    )


def test_empty_200_is_not_withheld_and_304_without_baseline_recovers_once():
    session = Mock(spec=requests.Session)
    session.get.side_effect = [response(304), response(200, b"", ETag='"new"')]
    attempts = fetch(session)
    assert [a.receipt.status for a in attempts] == ["http_error", "ok"]
    assert attempts[0].receipt.error_code == "missing_304_baseline"
    assert attempts[1].body == b""
    assert attempts[1].receipt.body_ref.sha256 == hashlib.sha256(b"").hexdigest()
    assert all(not call.kwargs["allow_redirects"] for call in session.get.call_args_list)
    assert all("If-None-Match" not in call.kwargs["headers"] for call in session.get.call_args_list)


def test_conditional_receipt_keeps_concrete_200_identity_and_validator():
    baseline = ArticleReceipt(
        receipt_id="old",
        source="tsr_data",
        requested_url=SOURCES["tsr_data"].entry_url,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        status="ok",
        http_status=200,
        etag='"old"',
        body_ref=BodyRef(receipt_id="old", sha256="a" * 64),
    )
    session = Mock(spec=requests.Session)
    session.get.return_value = response(304)
    (attempt,) = fetch(session, baseline=baseline)
    assert attempt.receipt.status == "not_modified"
    assert attempt.receipt.body_ref == baseline.body_ref
    assert attempt.body is None
    assert session.get.call_args.kwargs["headers"]["If-None-Match"] == '"old"'
    with pytest.raises(ValueError, match="representation"):
        fetch(
            session,
            baseline=baseline.model_copy(update={"requested_url": "https://other.invalid/"}),
        )


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 503])
def test_http_failures_do_not_retry_or_follow_and_return_cooldown(status):
    session = Mock(spec=requests.Session)
    session.get.return_value = response(
        status, **{"Retry-After": "7200", "Location": "https://other.invalid"}
    )
    (attempt,) = fetch(session)
    assert attempt.receipt.status == "http_error"
    assert session.get.call_count == 1
    if status in (429, 503):
        assert (
            attempt.receipt.cooldown_until - attempt.receipt.received_at
        ).total_seconds() == 7200


def test_decompressed_bytes_and_shared_request_budget_are_enforced():
    session = Mock(spec=requests.Session)
    session.get.return_value = response(200, b"x" * 501)
    budget = ArticleBudget.start(max_requests=1, max_bytes=1000, seconds=30)
    (attempt,) = fetch(session, budget=budget)
    assert attempt.receipt.status == "budget_exceeded"
    assert attempt.body is None
    assert budget.bytes_used == 501
    with pytest.raises(ArticleBudgetExceeded, match="requests"):
        fetch(session, budget=budget)
    assert session.get.call_count == 1


def test_source_allowlist_does_not_follow_cross_domain_canonical():
    session = Mock(spec=requests.Session)
    with pytest.raises(ValueError, match="allowlist"):
        fetch_article(
            session,
            source="minkabu_news",
            url="https://unverified.invalid/news/1",
            allowed_hosts=SOURCES["minkabu_news"].allowed_hosts,
            budget=ArticleBudget.start(max_requests=1, max_bytes=1000, seconds=30),
            max_response_bytes=500,
        )
    session.get.assert_not_called()
    document = parse_document(
        "minkabu_news",
        b'<link rel="canonical" href="https://unverified.invalid/news/1"><h1>Synthetic</h1><div class="news-body">body</div>',
        url="https://minkabu.jp/news/1",
    )
    assert document.canonical_url == "https://unverified.invalid/news/1"
    assert document.url == "https://minkabu.jp/news/1"


def test_tsr_item_revision_excludes_sort_but_includes_title_date_and_search_content():
    item = {
        "url": "/data/detail/synthetic.html",
        "title": "Synthetic",
        "sort": 1,
        "viewing_start_ymd": "2026-09-30 15:00:00",
        "list_text": "Summary",
        "free_word": ["native", "search"],
    }

    def parse(value):
        return parse_index(
            "tsr_data", json.dumps([value]).encode(), url=SOURCES["tsr_data"].entry_url
        )[0]

    first = parse(item)
    reordered = parse({**item, "sort": 2})
    assert first.metadata["source_item_sha256"] == reordered.metadata["source_item_sha256"]
    for key, value in [
        ("title", "Changed"),
        ("viewing_start_ymd", "2026-10-01 15:00:00"),
        ("free_word", ["changed"]),
    ]:
        assert (
            parse({**item, key: value}).metadata["source_item_sha256"]
            != first.metadata["source_item_sha256"]
        )
    assert first.content_scope == "summary"
    assert first.text == "Summary"
    assert (
        native_time(first.published_raw, source="tsr_data").timezone_origin
        == "provider_japan_local_time"
    )


def test_rss_empty_description_and_news_sitemap_do_not_claim_full_body_or_lastmod():
    rss = b"<rss><channel><item><title>Synthetic decision</title><link>http://www.boj.or.jp/synthetic.pdf</link><description/><pubDate>Tue, 01 Sep 2026 12:00:00 +0900</pubDate></item></channel></rss>"
    (item,) = parse_index("boj_news", rss, url=SOURCES["boj_news"].entry_url)
    assert item.content_scope == "headline" and item.text == ""
    assert item.metadata["fetch_url"] == "https://www.boj.or.jp/synthetic.pdf"
    assert item.article_id == "boj_news:https://www.boj.or.jp/synthetic.pdf"
    sitemap = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:n="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>https://media.rakuten-sec.net/articles/-/1</loc><n:news><n:title>Synthetic</n:title><n:publication_date>2026-10-01T00:00:00+09:00</n:publication_date></n:news></url></urlset>'
    (item,) = parse_index("toushiru", sitemap, url=SOURCES["toushiru"].entry_url)
    assert item.updated_raw is None
    assert item.metadata["revision_detection"] == "bounded_revalidation_required"
    assert native_time(None, source="boj_news").value is None


def test_rakuten_login_links_are_headlines_only_and_minkabu_overlap_deduplicates():
    html = b'<a href="https://www.rakuten-sec.co.jp/Direct_Login/?story=1">Synthetic headline</a>'
    (item,) = parse_index("rakuten_news", html, url=SOURCES["rakuten_news"].entry_url)
    assert item.content_scope == "headline" and SOURCES["rakuten_news"].max_bodies == 0
    html = b'<a href="/news/123">one</a><a href="/news/123">same</a><a rel="next" href="/news?page=2">next</a>'
    items = parse_index("minkabu_news", html, url=SOURCES["minkabu_news"].entry_url)
    assert len(items) == 1
    assert (
        next_index_url("minkabu_news", html, url=SOURCES["minkabu_news"].entry_url)
        == "https://minkabu.jp/news?page=2"
    )
    with pytest.raises(ValueError, match="not a confirmed empty"):
        parse_index("minkabu_news", b"<html>blocked</html>", url=SOURCES["minkabu_news"].entry_url)


def test_partial_html_does_not_include_navigation_or_claim_linked_pdf_read():
    html = b'<h1>Synthetic</h1><main><nav>ignore</nav><p>Decision</p><a href="/attachment.pdf">Annex</a></main>'
    item = parse_document("boj_news", html, url="https://www.boj.or.jp/synthetic.html")
    assert "ignore" not in item.text
    assert item.content_scope == "partial_text"
    assert item.metadata["attachments"] == ["https://www.boj.or.jp/attachment.pdf"]


def test_pdf_projection_records_exact_page_coordinates():
    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for value in (b"Synthetic decision one", b"Synthetic annex two"):
        page = writer.add_blank_page(width=400, height=400)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 12 Tf 20 300 Td (" + value + b") Tj ET")
        page[NameObject("/Contents")] = content
    output = BytesIO()
    writer.write(output)
    item = parse_document("boj_news", output.getvalue(), url="https://www.boj.or.jp/synthetic.pdf")
    spans = item.metadata["page_spans"]
    assert len(spans) == 2
    assert "Synthetic decision one" in item.text[spans[0]["start"] : spans[0]["end"]]
    assert "Synthetic annex two" in item.text[spans[1]["start"] : spans[1]["end"]]
    assert item.missing_parts == ("non_text_figures_and_layout",)


def test_orphan_304_is_returned_even_when_recovery_budget_is_exhausted():
    session = Mock(spec=requests.Session)
    session.get.return_value = response(304)
    (attempt,) = fetch(
        session, budget=ArticleBudget.start(max_requests=1, max_bytes=1000, seconds=30)
    )
    assert attempt.receipt.error_code == "missing_304_baseline"
    assert session.get.call_count == 1


def test_nhk_guid_is_shared_across_categories_and_description_remains_summary():
    def feed(marker):
        return f'<rss xmlns:nhk="http://news.web.nhk/rss/rss2.0/modules/nhknews/"><channel><item><title>Synthetic</title><link>https://news.web.nhk/newsweb/na/na-k10000000000000</link><guid isPermaLink="false">synthetic-guid</guid><description>Only synthetic summary</description><pubDate>Thu, 29 Oct 2026 12:00:00 +0900</pubDate><nhk:new>{marker}</nhk:new></item></channel></rss>'.encode()

    (political,) = parse_index(
        "nhk_news", feed("true"), url="https://news.web.nhk/n-data/conf/na/rss/cat4.xml"
    )
    (economic,) = parse_index(
        "nhk_news", feed("false"), url="https://news.web.nhk/n-data/conf/na/rss/cat5.xml"
    )
    assert political.article_id == economic.article_id == "nhk_news:synthetic-guid"
    assert political.metadata["source_item_sha256"] == economic.metadata["source_item_sha256"]
    assert political.content_scope == "summary"
    assert political.text == "Only synthetic summary"


def test_document_publication_date_keeps_provider_precision_and_origin():
    item = parse_document(
        "tsr_data",
        b'<h1>Synthetic</h1><div class="entry"><div class="entry_date">2026/10/29</div><p>Body</p></div>',
        url="https://www.tsr-net.co.jp/synthetic.html",
    )
    time = native_time(item.published_raw, source="tsr_data")
    assert time.raw == "2026/10/29" and time.value == "2026-10-29" and time.precision == "date"
    item = parse_document(
        "toushiru",
        b'<h1>Synthetic</h1><time class="article-meta__date" datetime="2026-10-29T08:00+09:00">date</time><div class="article-body">Body</div>',
        url="https://media.rakuten-sec.net/articles/-/synthetic",
    )
    time = native_time(item.published_raw, source="toushiru")
    assert time.precision == "minute" and time.timezone_origin == "explicit_offset"


def test_nhk_budget_is_one_provider_with_four_feeds_and_partial_body_contract():
    from baibai_engine.data_collection.providers.articles.native import NativeArticleUnsupported

    spec = SOURCES["nhk_news"]
    assert len(spec.entry_urls) == 4
    assert spec.max_pages == 4 and spec.max_bodies == 8 and spec.max_revalidate_bodies == 2
    assert spec.revalidate_hours == 48 and spec.min_interval_seconds == 1.0
    assert spec.response_limit("index") == 1024 * 1024
    assert spec.response_limit("document") == 2 * 1024 * 1024
    assert spec.max_bytes == 20 * 1024 * 1024 and spec.max_seconds == 120
    url = "https://news.web.nhk/newsweb/na/synthetic"
    with pytest.raises(NativeArticleUnsupported, match="unsupported"):
        parse_document("nhk_news", b"<main>page chrome and summary</main>", url=url)
    with pytest.raises(NativeArticleUnsupported, match="no text"):
        parse_document("nhk_news", b'<div itemprop="articleBody"></div>', url=url)
    body = parse_document(
        "nhk_news", b'<h1>Synthetic</h1><div itemprop="articleBody"><p>Body</p></div>', url=url
    )
    assert body.text == "Body" and body.content_scope == "partial_text"


def test_provider_interval_applies_to_every_request_without_real_sleep(monkeypatch):
    from baibai_engine.data_collection.transport import articles

    clock = [100.0]
    waits = []
    monkeypatch.setattr(articles.time, "monotonic", lambda: clock[0])

    def advance(seconds):
        waits.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(articles, "wait_for_read_retry", advance)
    budget = ArticleBudget.start(max_requests=3, max_bytes=1000, seconds=5, min_interval_seconds=1)
    budget.request()
    budget.request()
    budget.request()
    assert waits == [1.0, 1.0]
    assert budget.requests_used == 3
