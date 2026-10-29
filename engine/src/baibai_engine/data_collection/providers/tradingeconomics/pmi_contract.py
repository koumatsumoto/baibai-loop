"""Native public PMI article structure and identity, without transport or acquisition."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin

BASE_URL = "https://tradingeconomics.com"
STREAM_PATHS = {
    "jp_manufacturing": "/japan/manufacturing-pmi",
    "jp_services": "/japan/services-pmi",
    "us_manufacturing": "/united-states/manufacturing-pmi",
    "us_services": "/united-states/services-pmi",
}
STREAM_NAMES = {
    "jp_manufacturing": r"S&P Global Japan Manufacturing PMI",
    "jp_services": r"Japan[’']s S&P Global Services PMI Business Activity Index",
    "us_manufacturing": r"S&P Global US Manufacturing PMI",
    "us_services": r"S&P Global US Services PMI(?: Business Activity Index)?",
}
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


@dataclass(frozen=True, slots=True)
class PmiArticle:
    lead: str
    observed_at: date
    phase: str
    published_at: datetime
    modified_at: datetime


class _ArticleHtml(HTMLParser):
    """Only the main article's description; appended News Stream is not its body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.description_depth = 0
        self.paragraph = False
        self.paragraphs: list[str] = []
        self.parts: list[str] = []
        self.json_script = False
        self.json_parts: list[str] = []
        self.articles: list[dict[str, object]] = []
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self.links.add(str(attributes["href"]))
        if tag == "script" and attributes.get("type") == "application/ld+json":
            self.json_script = True
            self.json_parts = []
        if tag == "div":
            if self.description_depth:
                self.description_depth += 1
            elif "news_description" in (attributes.get("class") or "").split():
                self.description_depth = 1
        if self.description_depth and tag == "p":
            self.paragraph = True
            self.parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self.json_script:
            self.json_script = False
            try:
                value = json.loads("".join(self.json_parts))
            except ValueError:
                return
            if isinstance(value, dict) and value.get("@type") == "Article":
                self.articles.append(value)
        if tag == "p" and self.paragraph:
            self.paragraphs.append(" ".join("".join(self.parts).split()))
            self.paragraph = False
        if tag == "div" and self.description_depth:
            self.description_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.json_script:
            self.json_parts.append(data)
        if self.description_depth and self.paragraph:
            self.parts.append(data)


def _html(body: bytes) -> _ArticleHtml:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("TE PMI article is not UTF-8 HTML") from error
    parser = _ArticleHtml()
    parser.feed(text)
    return parser


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("TE PMI article requires an explicit publication timestamp")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("TE PMI publication timestamp requires a timezone")
    return stamp.astimezone(UTC)


def parse_article(body: bytes, *, stream: str, url: str) -> PmiArticle:
    """Bind native article country/index/period/phase; numeric interpretation is L2."""
    parser = _html(body)
    matching = [item for item in parser.articles if item.get("url") == url]
    if len(matching) != 1 or not parser.paragraphs:
        raise ValueError("TE PMI main article or its bound publication metadata is missing")
    info = matching[0]
    published = _timestamp(info.get("datePublished"))
    modified = _timestamp(info.get("dateModified", info.get("datePublished")))
    lead = parser.paragraphs[0]
    name = STREAM_NAMES[stream]
    match = re.search(
        rf"{name}\b(?!\s+(?:Output|Composite))(?P<statement>.*?)"
        rf"\b(?:in|for)\s+(?P<month>{'|'.join(MONTHS)})\s+(?P<year>\d{{4}})\b",
        lead,
    )
    if match is None or re.search(
        r"\b(?:Output|Composite|from|compared|versus|against|forecast)\b",
        match["statement"],
        re.IGNORECASE,
    ):
        raise ValueError("TE PMI main article does not identify the requested country/index/month")
    observed = date(int(match["year"]), MONTHS.index(match["month"]) + 1, 1)
    # An earlier flash comparison does not make the current article a flash release.
    flash = re.search(
        r"\A(?:The\s+)?Flash\b|preliminary estimates? showed|"
        r"according to (?:a )?(?:flash|preliminary) estimates?",
        lead,
        re.IGNORECASE,
    )
    final = re.search(
        r"\bconfirmed\b|\bfinal (?:reading|estimate|figures?)\b|"
        r"(?:from|above|below|matching)\s+(?:a |the )?(?:earlier )?"
        r"(?:flash|preliminary) (?:reading|estimate)",
        lead,
        re.IGNORECASE,
    )
    if flash:
        phase = "flash"
    elif final:
        phase = "final"
    else:
        raise ValueError("TE PMI main article does not establish flash/final phase")
    published_month = published.date().replace(day=1)
    if (phase == "flash" and published_month != observed) or (
        phase == "final" and published_month <= observed
    ):
        raise ValueError("TE PMI article month/phase conflicts with its publication date")
    return PmiArticle(lead, observed, phase, published, modified)


def article_urls(body: bytes, *, stream: str) -> tuple[str, ...]:
    path = STREAM_PATHS[stream]
    pattern = re.compile(re.escape(BASE_URL + path) + r"/news/\d+\Z")
    return tuple(
        sorted(
            {
                urljoin(BASE_URL, link)
                for link in _html(body).links
                if pattern.fullmatch(urljoin(BASE_URL, link))
            }
        )
    )


def request_key(url: str) -> str:
    return "te:" + url.rsplit("/", 1)[1]
