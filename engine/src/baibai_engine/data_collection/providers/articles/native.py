"""Native fields only. Numeric claims, attribution and semantic families belong to Observations."""

from __future__ import annotations

import io
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from pypdf import PdfReader

from baibai_engine.data_collection.providers.articles.contracts import NativeArticle
from baibai_engine.data_collection.providers.articles.html import parse_html, plain_text

PARSER_REVISION = "article-native-v1"
TEXT_REVISION = "article-text-v1"


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
    if source in {"boj_news", "mof_news", "toushiru"}:
        # ElementTree does not retrieve external entities. Explicitly reject DTDs.
        if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
            raise ValueError("article feed DTD/entities are unsupported")
        root = ET.fromstring(body)  # nosec B314
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
                    article_id=f"{source}:{link}",
                    locator=f"/item/{index}",
                    url=link,
                    title=plain_text(fields.get("title", "")),
                    text=summary,
                    content_scope="summary" if summary else "headline",
                    published_raw=fields.get("pubDate") or fields.get("date") or None,
                    metadata={"format": "rss", "fetch_url": _https(link)},
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


def parse_document(
    source: str, body: bytes, *, url: str, title: str = "", encoding: str = "utf-8"
) -> NativeArticle:
    if body.startswith(b"%PDF-"):
        reader = PdfReader(io.BytesIO(body))
        if reader.is_encrypted or len(reader.pages) > 100:
            raise ValueError("encrypted or oversized article PDF is unsupported")
        parts = [page.extract_text() or "" for page in reader.pages]
        if not any(part.strip() for part in parts):
            raise ValueError("article PDF has no extractable text")
        offsets = []
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
    canonical = next(
        (
            urljoin(url, node.attrs["href"])
            for node in nodes
            if node.tag == "link" and node.attrs.get("rel") == "canonical" and "href" in node.attrs
        ),
        None,
    )
    heading = next((node.text().strip() for node in nodes if node.tag == "h1"), title)
    classes = {
        "tsr_data": ("entry",),
        "tsr_flash": ("entry",),
        "toushiru": ("article-body",),
        "minkabu_news": ("news-body", "md_newsBody", "news_body"),
    }.get(source, ())
    region = next((node for node in nodes if any(node.has_class(name) for name in classes)), None)
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
    if region is None:
        raise ValueError("article body region is unsupported")
    metadata = {}
    for node in nodes:
        if node.tag == "meta" and node.attrs.get("property") in {
            "article:published_time",
            "article:modified_time",
            "article:author",
        }:
            metadata[node.attrs["property"]] = node.attrs.get("content", "")
    attachments = [
        urljoin(url, node.attrs["href"])
        for node in region.walk()
        if node.tag == "a" and ".pdf" in node.attrs.get("href", "").lower()
    ]
    metadata["attachments"] = attachments
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
        published_raw=metadata.get("article:published_time"),
        updated_raw=metadata.get("article:modified_time"),
        metadata=metadata,
    )
