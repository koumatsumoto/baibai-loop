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


SOURCES = {
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
        "boj_news", "https://www.boj.or.jp/rss/whatsnew.xml", frozenset({"www.boj.or.jp"})
    ),
    "mof_news": ArticleSource(
        "mof_news", "https://www.mof.go.jp/news.rss", frozenset({"www.mof.go.jp"})
    ),
}
