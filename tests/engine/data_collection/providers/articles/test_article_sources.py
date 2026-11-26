"""Offline checks for provider formats and saved article receipt identity."""

import json

import pytest

from baibai_engine.data_collection.providers.articles.contracts import SOURCES
from baibai_engine.data_collection.providers.articles.native import (
    NativeArticleUnsupported,
    is_index_locator,
    next_index_url,
    parse_document,
    parse_index,
    parser_revision_for,
)
from baibai_engine.data_collection.receipts.articles.contracts import ArticleReceipt
from baibai_engine.machine_analysis.observations.articles.projection import native_time


def test_native_index_locator_shapes_include_tsr_json_items_only_for_tsr():
    assert is_index_locator("tsr_data", "/0")
    assert is_index_locator("tsr_flash", "/12")
    assert not is_index_locator("tsr_data", "/html/body-region")
    assert not is_index_locator("nhk_news", "/0")
    assert is_index_locator("nhk_news", "/item/2")
    for source in ("boj_news", "mof_news", "nhk_news", "toushiru"):
        assert is_index_locator(source, "/url/0")


def test_cross_domain_canonical_does_not_change_document_url():
    document = parse_document(
        "minkabu_news",
        b'<link rel="canonical" href="https://unverified.invalid/news/1"><h1>Synthetic</h1><div class="news-body">body</div>',
        url="https://minkabu.jp/news/1",
    )
    assert document.canonical_url == "https://unverified.invalid/news/1"
    assert document.url == "https://minkabu.jp/news/1"


def test_minkabu_headed_card_selects_only_article_body_near_related_and_ads():
    url = "https://minkabu.jp/news/123"
    html = (
        b'<div id="contents"><div class="md_card md_box">'
        b'<div class="md_index_article"><h1>Synthetic headline</h1></div>'
        b'<div class="md_box fsize_m md_normalize"><p>First paragraph.</p>'
        b"<p>Second paragraph.</p></div></div>"
        b'<aside>Advertisement nearby</aside><div class="related"><p>Related story</p></div>'
        b"</div>"
    )
    article = parse_document("minkabu_news", html, url=url)
    assert article.title == "Synthetic headline"
    assert "First paragraph." in article.text
    assert "Second paragraph." in article.text
    assert "Related story" not in article.text
    assert "Advertisement" not in article.text
    assert article.content_scope == "partial_text"
    assert parser_revision_for("minkabu_news") == "article-native-minkabu-v3"
    assert parser_revision_for("tsr_data") == "article-native-v2"


@pytest.mark.parametrize(
    "html",
    [
        (
            b'<div id="contents"><div class="md_card md_box"><div class="md_index_article">'
            b'<h1>Synthetic</h1></div><div class="md_box fsize_m md_normalize"></div></div></div>'
        ),
        (
            b'<div id="contents"><div class="md_card md_box">'
            b'<div class="md_box fsize_m md_normalize">Unheaded body-like text</div></div></div>'
        ),
        (
            b'<div id="contents"><div class="md_card md_box"><div class="md_index_article">'
            b'<h1>Synthetic</h1></div><div class="md_box fsize_m md_normalize">One</div>'
            b'<div class="md_box fsize_m md_normalize">Two</div></div></div>'
        ),
    ],
)
def test_minkabu_new_shape_missing_or_ambiguous_body_is_unsupported(html):
    with pytest.raises(NativeArticleUnsupported, match="body region"):
        parse_document("minkabu_news", html, url="https://minkabu.jp/news/123")


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
    assert item.content_scope == "headline"
    assert item.text == ""
    assert item.metadata["fetch_url"] == "https://www.boj.or.jp/synthetic.pdf"
    assert item.article_id == "boj_news:https://www.boj.or.jp/synthetic.pdf"
    sitemap = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:n="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>https://media.rakuten-sec.net/articles/-/1</loc><n:news><n:title>Synthetic</n:title><n:publication_date>2026-10-01T00:00:00+09:00</n:publication_date></n:news></url></urlset>'
    (item,) = parse_index("toushiru", sitemap, url=SOURCES["toushiru"].entry_url)
    assert item.updated_raw is None
    assert item.metadata["revision_detection"] == "bounded_revalidation_required"
    assert native_time(None, source="boj_news").value is None


@pytest.mark.parametrize(
    "body",
    [
        b'<rss xmlns="urn:synthetic:rss"><channel/></rss>',
        b'<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><item><link>https://www.boj.or.jp/native.htm</link><dc:date>2026-11-22</dc:date></item></channel></rss>',
        b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><item><link>https://www.boj.or.jp/native.htm</link><dc:date>2026-11-22</dc:date></item></rdf:RDF>',
        b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://www.boj.or.jp/native.htm</loc></url></urlset>',
    ],
)
def test_explicit_rss_revision_does_not_change_clean_items_rdf_sitemap_or_empty(body):
    url = SOURCES["boj_news"].entry_url
    assert parse_index(
        "boj_news", body, url=url, rss_index_revision="rss-native-v1"
    ) == parse_index("boj_news", body, url=url)


def test_native_rss_index_cannot_use_extension_link_without_native_link():
    body = (
        b'<rss xmlns:ext="urn:synthetic:extension"><channel><item>'
        b"<ext:link>https://www.boj.or.jp/foreign.htm</ext:link></item></channel></rss>"
    )
    with pytest.raises(ValueError, match="feed item has no link"):
        parse_index(
            "boj_news", body, url=SOURCES["boj_news"].entry_url, rss_index_revision="rss-native-v1"
        )


def test_rakuten_login_links_are_headlines_only_and_minkabu_overlap_deduplicates():
    html = b'<a href="https://www.rakuten-sec.co.jp/Direct_Login/?story=1">Synthetic headline</a>'
    (item,) = parse_index("rakuten_news", html, url=SOURCES["rakuten_news"].entry_url)
    assert item.content_scope == "headline"
    assert SOURCES["rakuten_news"].max_bodies == 0
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
    assert time.raw == "2026/10/29"
    assert time.value == "2026-10-29"
    assert time.precision == "date"
    item = parse_document(
        "toushiru",
        b'<h1>Synthetic</h1><time class="article-meta__date" datetime="2026-10-29T08:00+09:00">date</time><div class="article-body">Body</div>',
        url="https://media.rakuten-sec.net/articles/-/synthetic",
    )
    time = native_time(item.published_raw, source="toushiru")
    assert time.precision == "minute"
    assert time.timezone_origin == "explicit_offset"


def test_nhk_budget_is_one_provider_with_four_feeds_and_public_summary_contract():
    from baibai_engine.data_collection.providers.articles.native import NativeArticleUnsupported

    spec = SOURCES["nhk_news"]
    assert len(spec.entry_urls) == 4
    assert spec.max_pages == 4
    assert spec.max_bodies == 8
    assert spec.max_revalidate_bodies == 2
    assert spec.revalidate_hours == 48
    assert spec.min_interval_seconds == 1.0
    assert spec.response_limit("index") == 1024 * 1024
    assert spec.response_limit("document") == 2 * 1024 * 1024
    assert spec.max_bytes == 20 * 1024 * 1024
    assert spec.max_seconds == 120
    url = "https://news.web.nhk/newsweb/na/synthetic"
    with pytest.raises(NativeArticleUnsupported, match="unsupported"):
        parse_document("nhk_news", b"<main>page chrome and summary</main>", url=url)
    with pytest.raises(NativeArticleUnsupported, match="description"):
        parse_document("nhk_news", b'<div itemprop="articleBody"></div>', url=url)
    with pytest.raises(NativeArticleUnsupported, match="description"):
        parse_document("nhk_news", b'<div itemprop="articleBody">Body-like text</div>', url=url)
    body = parse_document(
        "nhk_news",
        b'<main>Related stories and account UI</main><script type="application/ld+json">'
        b'{"@type":"NewsArticle","headline":{"@value":"Synthetic","@language":"ja"},'
        b'"description":{"@value":"Public short summary","@language":"ja"},'
        b'"identifier":"synthetic-id","datePublished":"2026-10-30T09:00:00+09:00",'
        b'"dateModified":"2026-10-30T10:00:00+09:00",'
        b'"mainEntityOfPage":{"@type":"WebPage",'
        b'"@id":"https://news.web.nhk/newsweb/na/synthetic"}}</script>',
        url=url,
    )
    assert body.text == "Public short summary"
    assert body.content_scope == "summary"
    assert body.missing_parts == ("full_article_body",)
    assert body.published_raw == "2026-10-30T09:00:00+09:00"
    assert body.updated_raw == "2026-10-30T10:00:00+09:00"
    assert body.metadata["identifier"] == "synthetic-id"
    assert body.metadata["headline_language"] == "ja"
    assert body.metadata["description_language"] == "ja"
    with pytest.raises(NativeArticleUnsupported, match="description"):
        parse_document(
            "nhk_news",
            b'<script type="application/ld+json">'
            b'{"@type":"NewsArticle","description":"Synthetic without page anchor"}</script>',
            url=url,
        )
    with pytest.raises(NativeArticleUnsupported, match="description"):
        parse_document(
            "nhk_news",
            b'<script type="application/ld+json">'
            b'{"@type":"NewsArticle","description":"Synthetic null anchor",'
            b'"mainEntityOfPage":{"@type":"WebPage","@id":null}}</script>',
            url=url,
        )
    with pytest.raises(NativeArticleUnsupported, match="description"):
        parse_document(
            "nhk_news",
            b'<script type="application/ld+json">'
            b'{"@type":"NewsArticle","description":{"@value":"Synthetic",'
            b'"@language":"ja"},"mainEntityOfPage":{"@type":"WebPage",'
            b'"@id":"https://news.web.nhk/newsweb/na/another"}}</script>',
            url=url,
        )
    with pytest.raises(NativeArticleUnsupported, match="description"):
        parse_document(
            "nhk_news",
            b'<script type="application/ld+json">'
            b'[{"@type":"NewsArticle","description":"one"},'
            b'{"@type":"NewsArticle","description":"related"}]</script>',
            url=url,
        )


@pytest.mark.parametrize(
    "body",
    [
        b'<!DOCTYPE rss [<!ENTITY value "expanded">]><rss><channel>&value;</channel></rss>',
        b"<! DOCTYPE rss><rss/>",
        '<!DOCTYPE rss [<!ENTITY value "expanded">]><rss/>'.encode("utf-16"),
        b"<rss>\x00</rss>",
        b"<rss><unclosed>",
    ],
)
def test_feed_rejects_dtd_entities_unsupported_encoding_and_malformed_xml(body):
    with pytest.raises(ValueError, match="article feed"):
        parse_index("boj_news", body, url=SOURCES["boj_news"].entry_url)


def test_feed_xml_has_an_offline_size_bound_and_accepts_utf8_bom():
    from baibai_engine.data_collection.providers.articles.native import MAX_FEED_XML_BYTES

    with pytest.raises(ValueError, match="size limit"):
        parse_index("boj_news", b" " * (MAX_FEED_XML_BYTES + 1), url=SOURCES["boj_news"].entry_url)
    assert (
        parse_index(
            "boj_news", b"\xef\xbb\xbf<rss><channel/></rss>", url=SOURCES["boj_news"].entry_url
        )
        == ()
    )


@pytest.mark.parametrize(
    "location",
    [
        "https://foreign.invalid/articles/-/2",
        "http://media.rakuten-sec.net/articles/-/2",
        "https://user@media.rakuten-sec.net/articles/-/2",
        "/login",
        "/articles/-/2?login=1",
        "/articles/-/2#section",
        "/articles/-/1",
    ],
)
def test_article_move_does_not_expand_document_scope(location):
    policy = SOURCES["toushiru"]
    old = "https://media.rakuten-sec.net/articles/-/1"
    assert policy.document_redirect(old, 301, location) is None
    assert policy.document_redirect(old, 403, "/articles/-/2") is None
    assert (
        SOURCES["minkabu_news"].document_redirect("https://minkabu.jp/news/1", 301, "/news/2")
        is None
    )


def test_optional_location_preserves_old_receipt_json_and_digest():
    from baibai_engine.data_collection.receipts.articles.contracts import digest_json

    old = {
        "receipt_id": "old",
        "source": "toushiru",
        "requested_url": "https://media.rakuten-sec.net/articles/-/1",
        "received_at": "2026-11-02T00:00:00Z",
        "status": "http_error",
        "http_status": 301,
    }
    receipt = ArticleReceipt.model_validate(old)
    saved = receipt.model_dump(mode="json")
    assert "location" not in saved
    assert receipt.sha256 == digest_json(saved)
    restored = ArticleReceipt.model_validate_json(json.dumps(saved))
    assert restored.sha256 == receipt.sha256
    with_location = receipt.model_copy(update={"location": "/articles/-/2"})
    assert with_location.sha256 != receipt.sha256


@pytest.mark.parametrize("source", ["bbc_news", "fed_news", "ecb_news"])
def test_xml_decoded_ampersand_at_end_of_headline_is_flushed(source):
    feed = b"<rss><channel><item><link>https://example.test/policy</link><title>Synthetic policy (Q&amp;A)</title></item></channel></rss>"
    (article,) = parse_index(source, feed, url="https://example.test/rss")
    assert article.title == "Synthetic policy (Q&A)"
    assert article.content_scope == "headline"
    assert article.text == ""


def test_html_walk_and_text_preserve_native_order_and_skipped_regions():
    from baibai_engine.data_collection.providers.articles.html import parse_html

    tree = parse_html(
        "lead<p>A<span>B</span><br>C</p><script>script</script><style>style</style>"
        "<nav><p>navigation</p></nav><aside>aside</aside><ul><li>D<div>E</div>F</li></ul>tail"
    )
    assert [node.tag for node in tree.walk()] == [
        "root",
        "p",
        "span",
        "br",
        "script",
        "style",
        "nav",
        "p",
        "aside",
        "ul",
        "li",
        "div",
    ]
    assert tree.text() == "leadAB\nC\nDE\nF\ntail"
    for node in tree.walk():
        if node.tag in {"script", "style", "nav", "aside"}:
            assert node.text() == ""


@pytest.mark.parametrize("depth", [1200, 2400])
def test_deep_html_walk_and_text_do_not_depend_on_python_recursion_depth(depth):
    from baibai_engine.data_collection.providers.articles.html import parse_html

    tree = parse_html("<div>" * depth + "合成本文" + "</div>" * depth + "末尾")
    assert [node.tag for node in tree.walk()] == ["root", *(["div"] * depth)]
    assert tree.text() == "合成本文" + "\n" * depth + "末尾"


@pytest.mark.parametrize("source", ["tsr_data", "boj_news", "minkabu_news"])
def test_deep_html_projects_native_text_without_changing_scope(source):
    nested = "<div>" * 1200 + "Synthetic text" + "</div>" * 1200
    if source == "tsr_data":
        body = json.dumps(
            [{"url": "/synthetic.htm", "title": "Synthetic", "list_text": nested}]
        ).encode()
        (item,) = parse_index(source, body, url=SOURCES[source].entry_url)
        assert item.content_scope == "summary"
        assert item.text == "Synthetic text"
    elif source == "minkabu_news":
        body = ("<a href='/news/123'>" + nested + "</a>").encode()
        (item,) = parse_index(source, body, url=SOURCES[source].entry_url)
        assert item.content_scope == "headline"
        assert item.title == "Synthetic text"
        assert item.text == ""
    else:
        item = parse_document(
            source,
            ("<main>" + nested + "</main>").encode(),
            url="https://www.boj.or.jp/synthetic.htm",
        )
        assert item.content_scope == "partial_text"
        assert item.text == "Synthetic text"
        assert item.missing_parts == ("images_tables_attachments_or_other_pages",)
