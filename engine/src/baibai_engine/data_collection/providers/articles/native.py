"""Native fields only. Numeric claims, attribution and semantic families belong to Observations."""

from __future__ import annotations

import csv
import io
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET  # nosec B405 -- bounded _feed_root rejects DTD/entities

from pydantic import JsonValue
from pypdf import PdfReader

from baibai_engine.data_collection.providers.articles.contracts import (
    SOURCES,
    NativeArticle,
    NativeLink,
)
from baibai_engine.data_collection.providers.articles.html import Node, parse_html, plain_text
from baibai_engine.data_collection.providers.articles.workbook import parse_workbook
from baibai_engine.data_collection.receipts.articles.contracts import digest_json

PARSER_REVISION = "article-native-v1"
MINKABU_PARSER_REVISION = "article-native-minkabu-v2"
BOJ_PARSER_REVISION = "article-native-boj-v2"
MOF_PARSER_REVISION = "article-native-mof-v6"
TEXT_REVISION = "article-text-v1"
MAX_FEED_XML_BYTES = 4 * 1024 * 1024
MAX_NHK_JSON_LD_BYTES = 256 * 1024
_MOF_YIELD_CSV_URLS = frozenset(
    {
        "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv",
        "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv",
    }
)


class NativeArticleUnsupported(ValueError):
    """Saved source has no supported article body region; it is not empty success."""


def parser_revision_for(source: str) -> str:
    """Keep other provider observations on their saved parser identity."""
    if source == "minkabu_news":
        return MINKABU_PARSER_REVISION
    if source == "boj_news":
        return BOJ_PARSER_REVISION
    if source == "mof_news":
        return MOF_PARSER_REVISION
    return PARSER_REVISION


def _minkabu_card_body(nodes: list[Node]) -> Node | None:
    """Select the sole body in a headed article card under the public contents root."""
    contents = [node for node in nodes if node.attrs.get("id") == "contents"]
    candidates: list[Node] = []
    for root in contents:
        for card in root.walk():
            if not (card.has_class("md_card") and card.has_class("md_box")):
                continue
            has_heading = any(
                section.has_class("md_index_article")
                and any(node.tag == "h1" and node.text().strip() for node in section.walk())
                for section in card.walk()
            )
            if not has_heading:
                continue
            candidates.extend(
                node
                for node in card.walk()
                if node.tag == "div"
                and node.has_class("md_box")
                and node.has_class("fsize_m")
                and node.has_class("md_normalize")
            )
    if len(candidates) > 1:
        raise NativeArticleUnsupported("Minkabu article body region is ambiguous")
    return candidates[0] if candidates else None


def is_index_locator(source: str, locator: str) -> bool:
    """Accept only native index item positions emitted by this provider parser."""
    if source in {"tsr_data", "tsr_flash"}:
        pattern = r"/(?:0|[1-9][0-9]*)"
    elif source in {"boj_news", "mof_news", "nhk_news", "toushiru"}:
        pattern = r"/(?:item|url)/(?:0|[1-9][0-9]*)"
    elif source in {"minkabu_news", "rakuten_news"}:
        pattern = r"/a/(?:0|[1-9][0-9]*)"
    else:
        return False
    return re.fullmatch(pattern, locator) is not None


def _feed_root(body: bytes) -> ET.Element:
    """Bounded UTF-8 RSS/sitemap input, following the existing EDINET XML boundary."""
    if len(body) > MAX_FEED_XML_BYTES:
        raise ValueError("article feed XML size limit")
    try:
        text = body.decode("utf-8-sig")
        if "\x00" in text:
            raise ValueError("article feed XML encoding unsupported")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
            raise ValueError("article feed DTD/entities are unsupported")
        return ET.fromstring(text)  # nosec B314 -- bounded UTF-8, no DTD/entities
    except (UnicodeError, ET.ParseError) as exc:
        raise ValueError("article feed XML invalid") from exc


def _https(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme == "http" and parsed.hostname in {"www.boj.or.jp", "www.mof.go.jp"}:
        return urlunsplit(parsed._replace(scheme="https"))
    return url


def parse_index(
    source: str, body: bytes, *, url: str, encoding: str = "utf-8"
) -> tuple[NativeArticle, ...]:
    if source in {"tsr_data", "tsr_flash"}:
        entries = json.loads(body.decode("utf-8-sig"))
        if not isinstance(entries, list):
            raise ValueError("TSR index must be a native array")
        result = []
        for index, item in enumerate(entries):
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("url"), str)
                or not isinstance(item.get("title"), str)
            ):
                raise ValueError("TSR native article identity/title missing")
            original_url = urljoin(url, item["url"])
            summary = plain_text(str(item.get("list_text") or ""))
            # free_word is a search index, not a guaranteed verbatim body.
            metadata = {
                key: item[key]
                for key in (
                    "main_tag",
                    "prefectures",
                    "list_industry",
                    "industry",
                    "bankruptcy_situation",
                    "debt_amount",
                )
                if key in item
            }
            metadata["source_item_sha256"] = digest_json(
                {key: value for key, value in item.items() if key != "sort"}
            )
            result.append(
                NativeArticle(
                    article_id=f"{source}:{original_url}",
                    locator=f"/{index}",
                    url=original_url,
                    title=plain_text(item["title"]),
                    text=summary,
                    content_scope="summary" if summary else "headline",
                    published_raw=item.get("viewing_start_ymd") or None,
                    metadata=metadata,
                )
            )
        return tuple(result)
    if source in {"boj_news", "mof_news", "toushiru", "nhk_news"}:
        root = _feed_root(body)
        result = []
        if root.tag.rsplit("}", 1)[-1] == "urlset":
            for index, entry in enumerate(root):
                fields = {node.tag.rsplit("}", 1)[-1]: node.text or "" for node in entry.iter()}
                link = fields.get("loc")
                if not link:
                    raise ValueError("sitemap entry has no loc")
                result.append(
                    NativeArticle(
                        article_id=f"{source}:{link}",
                        locator=f"/url/{index}",
                        url=link,
                        title=fields.get("title", ""),
                        text="",
                        content_scope="headline",
                        published_raw=fields.get("publication_date") or None,
                        updated_raw=fields.get("lastmod") or None,
                        metadata={
                            "format": "news_sitemap",
                            "source_item_sha256": digest_json(fields),
                            "revision_detection": "bounded_revalidation_required",
                        },
                    )
                )
            return tuple(result)
        if root.tag.rsplit("}", 1)[-1] not in {"rss", "RDF"}:
            raise ValueError("unsupported article feed root")
        for index, entry in enumerate(
            node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "item"
        ):
            fields = {node.tag.rsplit("}", 1)[-1]: node.text or "" for node in entry}
            link = fields.get("link")
            if not link:
                raise ValueError("feed item has no link")
            summary = plain_text(fields.get("description", ""))
            result.append(
                NativeArticle(
                    article_id=f"{source}:{fields.get('guid') or _https(link)}"
                    if source == "nhk_news"
                    else f"{source}:{_https(link)}",
                    locator=f"/item/{index}",
                    url=link,
                    title=plain_text(fields.get("title", "")),
                    text=summary,
                    content_scope="summary" if summary else "headline",
                    published_raw=fields.get("pubDate") or fields.get("date") or None,
                    metadata={
                        "format": "rss",
                        "fetch_url": _https(link),
                        "source_item_sha256": digest_json(
                            {
                                key: value
                                for key, value in fields.items()
                                if source != "nhk_news" or key not in {"new", "category"}
                            }
                        ),
                        "guid": fields.get("guid"),
                    },
                )
            )
        return tuple(result)
    tree = parse_html(body.decode(encoding))
    result = []
    seen = set()
    for index, node in enumerate(tree.walk()):
        if node.tag != "a":
            continue
        href = urljoin(url, node.attrs.get("href", ""))
        if source == "minkabu_news":
            accepted = bool(re.fullmatch(r"/news/\d+", urlsplit(href).path))
        elif source == "rakuten_news":
            accepted = "Direct_Login" in href
        else:
            raise ValueError("unsupported article source")
        if not accepted or href in seen:
            continue
        seen.add(href)
        result.append(
            NativeArticle(
                article_id=f"{source}:{href}",
                locator=f"/a/{index}",
                url=href,
                title=node.text().strip(),
                text="",
                content_scope="headline",
                metadata={"body_access": "not_requested"},
            )
        )
    if not result:
        raise ValueError("article index shape not recognized; not a confirmed empty result")
    return tuple(result)


def next_index_url(source: str, body: bytes, *, url: str, encoding: str = "utf-8") -> str | None:
    if source != "minkabu_news":
        return None
    for node in parse_html(body.decode(encoding)).walk():
        if node.tag == "a" and (
            "next" in node.attrs.get("rel", "").split()
            or node.text().strip() in {"次へ", "次", "次のページ"}
        ):
            candidate = urljoin(url, node.attrs.get("href", ""))
            if urlsplit(candidate).hostname == "minkabu.jp" and urlsplit(candidate).path == "/news":
                return candidate
    return None


def _nhk_public_description(
    nodes: list[Node], *, url: str, title: str, canonical: str | None
) -> NativeArticle:
    """The public NewsArticle description is a summary, never a verified full body."""

    def text_value(value: object) -> tuple[str, str | None] | None:
        if isinstance(value, str):
            return (plain_text(value), None)
        if not isinstance(value, dict):
            return None
        raw = value.get("@value")
        language = value.get("@language")
        if (
            not isinstance(raw, str)
            or not isinstance(language, str)
            or len(language) > 35
            or (language.lower() != "ja" and not language.lower().startswith("ja-"))
        ):
            return None
        return (plain_text(raw), language)

    accepted_urls = {url}
    if canonical:
        accepted_urls.add(canonical)
    candidates = []
    for node in nodes:
        if node.tag != "script" or node.attrs.get("type", "").lower() != "application/ld+json":
            continue
        raw = "".join(child for child in node.children if isinstance(child, str))
        if len(raw.encode("utf-8")) > MAX_NHK_JSON_LD_BYTES:
            continue
        try:
            document = json.loads(raw)
        except (ValueError, TypeError):
            continue
        roots = document if isinstance(document, list) else [document]
        for root in roots:
            if not isinstance(root, dict):
                continue
            graph = root.get("@graph")
            entries = graph if isinstance(graph, list) else [root]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                kinds = entry.get("@type")
                if "NewsArticle" not in (kinds if isinstance(kinds, list) else [kinds]):
                    continue
                description = text_value(entry.get("description"))
                if description is None or not description[0]:
                    continue
                linked_url = entry.get("url")
                linked = (
                    isinstance(linked_url, str) and bool(linked_url) and linked_url in accepted_urls
                )
                if linked_url is not None and not linked:
                    continue
                main_entity = entry.get("mainEntityOfPage")
                page_linked = False
                if main_entity is not None:
                    if not isinstance(main_entity, dict) or main_entity.get("@type") != "WebPage":
                        continue
                    page_id = main_entity.get("@id")
                    page_linked = (
                        isinstance(page_id, str) and bool(page_id) and page_id in accepted_urls
                    )
                    if not page_linked:
                        continue
                if not (linked or page_linked):
                    continue
                candidates.append((entry, description))
    if len(candidates) != 1:
        raise NativeArticleUnsupported("NHK public NewsArticle description is unsupported")
    entry, description = candidates[0]
    headline = text_value(entry.get("headline"))
    metadata: dict[str, JsonValue] = {"format": "newsarticle_jsonld_description"}
    if description[1] is not None:
        metadata["description_language"] = description[1]
    if headline is not None and headline[1] is not None:
        metadata["headline_language"] = headline[1]
    identifier = entry.get("identifier")
    if isinstance(identifier, str):
        metadata["identifier"] = identifier
    return NativeArticle(
        article_id=f"nhk_news:{url}",
        locator="/html/newsarticle-description",
        url=url,
        title=title or (headline[0] if headline is not None else ""),
        text=description[0],
        content_scope="summary",
        canonical_url=canonical,
        missing_parts=("full_article_body",),
        published_raw=entry.get("datePublished")
        if isinstance(entry.get("datePublished"), str)
        else None,
        updated_raw=entry.get("dateModified")
        if isinstance(entry.get("dateModified"), str)
        else None,
        metadata=metadata,
    )


def _mof_redirect_notice(nodes: list[Node], *, url: str, title: str) -> NativeArticle | None:
    """本省の明示案内をsourceの許可host内で一段だけ読む。案内は本文とは別受領。"""
    origin = urlsplit(url)
    if origin.hostname != "www.mof.go.jp" or not origin.path.startswith(
        ("/insideLink/", "/outside_link/")
    ):
        return None
    refresh = [
        node.attrs.get("content", "")
        for node in nodes
        if node.tag == "meta" and node.attrs.get("http-equiv", "").lower() == "refresh"
    ]
    if not refresh:
        return None
    match = re.fullmatch(r"\s*0(?:\.0+)?\s*;\s*url\s*=\s*(.+?)\s*", refresh[0], re.I)
    if len(refresh) != 1 or match is None:
        raise NativeArticleUnsupported("MOF redirect notice is ambiguous or delayed")
    target = urljoin(url, match.group(1).strip("\"'"))
    parsed = urlsplit(target)
    if (
        not SOURCES["mof_news"].allows_url(target)
        or parsed.path.startswith(("/insideLink/", "/outside_link/"))
        or target == url
    ):
        raise NativeArticleUnsupported("MOF redirect target is outside its document boundary")
    return NativeArticle(
        article_id=f"mof_news:{url}",
        locator="/html/meta-refresh",
        url=url,
        title=title,
        text="",
        content_scope="unavailable",
        missing_parts=("redirect_target_body_is_a_separate_receipt",),
        metadata={"redirect_target_url": target},
    )


def _mof_yield_csv(body: bytes, *, url: str, title: str) -> NativeArticle:
    # These two MOF tables use CP932, as in the existing mof_jgb source owner.
    # Preserve the table text; maturity/date/value interpretation remains L2 work.
    text = body.decode("cp932")
    try:
        rows = list(csv.reader(io.StringIO(text), strict=True))
    except csv.Error as error:
        raise NativeArticleUnsupported("invalid MOF yield CSV") from error
    headers = [row for row in rows if row and row[0] == "基準日"]
    if len(headers) != 1 or len(headers[0]) < 2:
        raise NativeArticleUnsupported("MOF yield CSV has no unique native header")
    return NativeArticle(
        article_id=f"mof_news:{url}",
        locator="/csv",
        url=url,
        title=title or (rows[0][0] if rows and rows[0] else ""),
        text=text,
        content_scope="full_text",
        metadata={"format": "csv", "encoding": "cp932", "row_count": len(rows)},
    )


def _document_region(source: str, nodes: list[Node], *, url: str, title: str) -> tuple[Node, str]:
    heading = next(
        (text for node in nodes if node.tag == "h1" if (text := node.text().strip())), title
    )
    classes = {
        "tsr_data": ("entry",),
        "tsr_flash": ("entry",),
        "toushiru": ("article-body",),
        "minkabu_news": ("news-body", "md_newsBody", "news_body"),
    }.get(source, ())
    region = next((node for node in nodes if any(node.has_class(name) for name in classes)), None)
    if region is None and source == "minkabu_news":
        region = next((node for node in nodes if node.attrs.get("itemprop") == "articleBody"), None)
    if region is None and source == "minkabu_news":
        region = _minkabu_card_body(nodes)
    if region is None and source in {"boj_news", "mof_news"}:
        region = next(
            (
                node
                for node in nodes
                if node.attrs.get("id") in {"main", "content", "main_contents"}
                or node.tag == "main"
            ),
            None,
        )
    if region is None and source == "mof_news":
        location = urlsplit(url)
        # 財務局と財務総研の確認済み本文領域だけを使い、ページ全体へfallbackしない。
        region_id = (
            "mainArea"
            if location.hostname == "lfb.mof.go.jp"
            else "contents-wrap"
            if location.hostname == "www.mof.go.jp" and location.path.startswith("/pri/")
            else None
        )
        regions = [node for node in nodes if region_id and node.attrs.get("id") == region_id]
        if len(regions) == 1:
            region = regions[0]
            heading = next(
                (
                    text
                    for node in region.walk()
                    if node.tag in {"h1", "h2"} and (text := node.text().strip())
                ),
                heading,
            )
    if region is None:
        raise NativeArticleUnsupported("article body region is unsupported")
    if source == "minkabu_news" and not region.text().strip():
        raise NativeArticleUnsupported("Minkabu article body region has no text")
    return region, heading


def _attachment_links(region: Node, url: str) -> tuple[NativeLink, ...]:
    return tuple(
        NativeLink(f"/html/body-region/a/{index}", urljoin(url, node.attrs["href"]))
        for index, node in enumerate(region.walk())
        if node.tag == "a" and ".pdf" in node.attrs.get("href", "").lower()
    )


def _rolling_reference(source: str, url: str) -> bool:
    # A rolling listing keeps references without requesting an unbounded archive.
    parsed = urlsplit(url)
    return source == "mof_news" and (parsed.hostname, parsed.path) in {
        ("www.mof.go.jp", "/pri/research/seminar/lmeeting.htm"),
        ("www.customs.go.jp", "/toukei/latest/index.htm"),
    }


def discovery_revision_for(source: str) -> str:
    if source not in {"boj_news", "mof_news"}:
        raise ValueError("source does not discover nested article documents")
    return f"article-links-{source}-v1"


def discover_links(
    source: str, body: bytes, *, url: str, encoding: str = "utf-8"
) -> tuple[NativeLink, ...]:
    """Discover explicit native links without requiring a successful body projection.

    No GET, timestamps or progress live here. The caller retains the exact input
    and atomically admits children before considering their parent processed.
    """
    discovery_revision_for(source)
    if (
        body.startswith(b"%PDF-")
        or (source == "boj_news" and body.startswith(b"PK\x03\x04"))
        or (source == "mof_news" and url in _MOF_YIELD_CSV_URLS)
    ):
        return ()
    nodes = parse_html(body.decode(encoding)).walk()
    if source == "mof_news":
        notice = _mof_redirect_notice(nodes, url=url, title="")
        if notice is not None:
            target = notice.metadata["redirect_target_url"]
            assert isinstance(target, str)
            return (NativeLink(notice.locator, target),)
    region, _ = _document_region(source, nodes, url=url, title="")
    return () if _rolling_reference(source, url) else _attachment_links(region, url)


def parse_document(
    source: str, body: bytes, *, url: str, title: str = "", encoding: str = "utf-8"
) -> NativeArticle:
    if source == "mof_news" and url in _MOF_YIELD_CSV_URLS:
        return _mof_yield_csv(body, url=url, title=title)
    if source == "boj_news" and body.startswith(b"PK\x03\x04"):
        return parse_workbook(body, source=source, url=url, title=title)
    if body.startswith(b"%PDF-"):
        reader = PdfReader(io.BytesIO(body))
        # MOF public PDFs can carry an encryption dictionary yet open normally
        # without a password. Let PdfReader reject genuinely unreadable pages;
        # do not equate the dictionary's presence with inaccessible content.
        if (reader.is_encrypted and source != "mof_news") or len(reader.pages) > 100:
            raise ValueError("encrypted or oversized article PDF is unsupported")
        parts = [page.extract_text() or "" for page in reader.pages]
        if not any(part.strip() for part in parts):
            raise ValueError("article PDF has no extractable text")
        offsets: list[JsonValue] = []
        offset = 0
        for page, part in enumerate(parts):
            offsets.append({"page": page + 1, "start": offset, "end": offset + len(part)})
            offset += len(part) + 1
        return NativeArticle(
            article_id=f"{source}:{url}",
            locator="/pdf",
            url=url,
            title=title,
            text="\n".join(parts),
            content_scope="partial_text",
            missing_parts=("non_text_figures_and_layout",),
            metadata={"page_spans": offsets},
        )
    tree = parse_html(body.decode(encoding))
    nodes = tree.walk()
    if source == "mof_news":
        notice = _mof_redirect_notice(nodes, url=url, title=title)
        if notice is not None:
            return notice
    canonical = next(
        (
            urljoin(url, node.attrs["href"])
            for node in nodes
            if node.tag == "link" and node.attrs.get("rel") == "canonical" and "href" in node.attrs
        ),
        None,
    )
    if source == "nhk_news":
        return _nhk_public_description(nodes, url=url, title=title, canonical=canonical)
    region, heading = _document_region(source, nodes, url=url, title=title)
    date_fields: dict[str, str] = {}
    for node in nodes:
        if node.tag == "meta" and node.attrs.get("property") in {
            "article:published_time",
            "article:modified_time",
            "article:author",
        }:
            date_fields[node.attrs["property"]] = node.attrs.get("content", "")
    if source in {"tsr_data", "tsr_flash"}:
        published = next(
            (node.text().strip() for node in nodes if node.has_class("entry_date")), None
        )
        if published and "article:published_time" not in date_fields:
            date_fields["article:published_time"] = published
    if source == "toushiru":
        published = next(
            (
                node.attrs.get("datetime")
                for node in nodes
                if node.tag == "time" and node.has_class("article-meta__date")
            ),
            None,
        )
        if published and "article:published_time" not in date_fields:
            date_fields["article:published_time"] = published
    metadata: dict[str, JsonValue] = dict(date_fields)
    metadata["attachments"] = [link.url for link in _attachment_links(region, url)]
    if _rolling_reference(source, url):
        metadata["attachment_scope"] = "rolling_listing_reference_only"
    # Deliberately conservative: image tables, pagination and linked papers were not read.
    return NativeArticle(
        article_id=f"{source}:{url}",
        locator="/html/body-region",
        url=url,
        title=heading,
        text=region.text().strip(),
        content_scope="partial_text",
        canonical_url=canonical,
        missing_parts=("images_tables_attachments_or_other_pages",),
        published_raw=date_fields.get("article:published_time"),
        updated_raw=date_fields.get("article:modified_time"),
        metadata=metadata,
    )
