"""Finite public article routes and provider-native index fields."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import Field, JsonValue

from baibai_engine.data_collection.receipts.articles.contracts import ArticleValue


class NativeArticle(ArticleValue):
    article_id: str
    locator: str
    url: str
    title: str
    text: str = Field(repr=False)
    content_scope: Literal["headline", "summary", "full_text", "partial_text", "unavailable"]
    published_raw: str | None = None
    updated_raw: str | None = None
    canonical_url: str | None = None
    missing_parts: tuple[str, ...] = ()
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True)
class ArticleSource:
    source_id: str
    entry_url: str
    allowed_hosts: frozenset[str]
    max_pages: int = 1
    max_bodies: int = 8
    max_response_bytes: int = 4_000_000
    max_bytes: int = 24_000_000
    max_seconds: int = 120
    additional_entry_urls: tuple[str, ...] = ()
    max_feed_bytes: int | None = None
    max_document_bytes: int | None = None
    min_interval_seconds: float = 0.0
    max_revalidate_bodies: int = 2
    revalidate_hours: int = 48

    @property
    def entry_urls(self) -> tuple[str, ...]:
        return (self.entry_url, *self.additional_entry_urls)

    def response_limit(self, kind: Literal["index", "document"]) -> int:
        limit = self.max_feed_bytes if kind == "index" else self.max_document_bytes
        return limit if limit is not None else self.max_response_bytes


SOURCES = {
    "nhk_news": ArticleSource(
        "nhk_news",
        "https://news.web.nhk/n-data/conf/na/rss/cat4.xml",
        frozenset({"news.web.nhk"}),
        max_pages=4,
        max_bodies=8,
        max_response_bytes=2 * 1024 * 1024,
        max_bytes=20 * 1024 * 1024,
        max_seconds=120,
        min_interval_seconds=1.0,
        max_feed_bytes=1024 * 1024,
        max_document_bytes=2 * 1024 * 1024,
        additional_entry_urls=(
            "https://news.web.nhk/n-data/conf/na/rss/cat5.xml",
            "https://news.web.nhk/n-data/conf/na/rss/cat6.xml",
            "https://news.web.nhk/n-data/conf/na/rss/cat0.xml",
        ),
    ),
    "tsr_data": ArticleSource(
        "tsr_data",
        "https://www.tsr-net.co.jp/data/json/search-2.json",
        frozenset({"www.tsr-net.co.jp"}),
        max_response_bytes=12_000_000,
    ),
    "tsr_flash": ArticleSource(
        "tsr_flash",
        "https://www.tsr-net.co.jp/news/tsr/json/search.json",
        frozenset({"www.tsr-net.co.jp"}),
        max_response_bytes=12_000_000,
    ),
    "minkabu_news": ArticleSource(
        "minkabu_news", "https://minkabu.jp/news", frozenset({"minkabu.jp"}), max_pages=3
    ),
    # The public iframe is the actual finite headline input. Login links are identities only.
    "rakuten_news": ArticleSource(
        "rakuten_news",
        "https://www.trkd-asia.com/rakutensec/news.jsp?ind=1&ric=0",
        frozenset({"www.trkd-asia.com"}),
        max_bodies=0,
    ),
    "toushiru": ArticleSource(
        "toushiru",
        "https://media.rakuten-sec.net/list/feed/rss4googlenews",
        frozenset({"media.rakuten-sec.net"}),
    ),
    "boj_news": ArticleSource(
        "boj_news",
        "https://www.boj.or.jp/rss/whatsnew.xml",
        frozenset({"www.boj.or.jp"}),
        # Published financial-system reports exceed the generic HTML response cap.
        # Keep the feed and total source budgets unchanged.
        max_document_bytes=12_000_000,
    ),
    "mof_news": ArticleSource(
        "mof_news",
        "https://www.mof.go.jp/news.rss",
        # 本省RSSのinsideLinkが案内する財務局も同じsourceの明示された取得範囲。
        # 任意subdomainへ広げず、案内と転送先を別受領として保持する。
        frozenset({"www.mof.go.jp", "lfb.mof.go.jp"}),
    ),
}
