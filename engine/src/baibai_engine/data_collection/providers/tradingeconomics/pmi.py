"""Bounded public PMI article reads; never traverse private/chart API endpoints."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin

from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.provider_contracts import ProviderSpec
from baibai_engine.data_collection.receipts.native_sources.documents import SourceDocument
from baibai_engine.data_collection.transport.context import (
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderFetchStoppedError,
    SourceWithheldError,
    fetch_bytes,
)

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
_MAX_HTML_BYTES = 4 * 1024 * 1024


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


class TradingEconomicsPmiSource:
    # Retained official PDF history shares the unchanged measurement source IDs.
    spec = ProviderSpec(name="tradingeconomics_pmi")
    name = spec.name

    def fetch_documents(
        self,
        source: SourceDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None,
        existing: Mapping[str, SourceDocument],
        force: bool,
        recheck_existing: bool = True,
        selected_urls: tuple[str, ...] | None = None,
    ) -> Iterator[SourceDocument]:
        stream = source.provider_series_id
        known = {
            document.source_url
            for document in existing.values()
            if document.metadata.get("delivery_provider") == "tradingeconomics"
            and any(
                isinstance(month, str) and start.isoformat() <= month <= end.isoformat()
                for month in (
                    document.metadata.get("observed_at"),
                    document.metadata.get("requested_observed_at"),
                )
            )
        }
        failures: list[str] = []
        if selected_urls is not None:
            # Explicit finite input, never an automatic alternative after a refusal.
            pattern = re.compile(re.escape(BASE_URL + STREAM_PATHS[stream]) + r"/news/\d+\Z")
            if not selected_urls or any(not pattern.fullmatch(url) for url in selected_urls):
                raise ValueError("explicit TE PMI article URLs must belong to the requested stream")
            discovered = selected_urls
            known = set()
        else:
            try:
                index = fetch_bytes(
                    session,
                    BASE_URL + STREAM_PATHS[stream],
                    params=None,
                    max_bytes=_MAX_HTML_BYTES,
                    context=context,
                )
                discovered = article_urls(index, stream=stream)
                if not discovered:
                    failures.append("TE PMI index has no public article links")
            except ProviderFetchStoppedError:
                raise
            except SourceWithheldError as error:
                raise ProviderFetchStoppedError(str(error)) from error
            except IndicatorsProviderError as error:
                discovered = ()
                failures.append(str(error))
        latest = dict(existing)
        for url in sorted(known | set(discovered)):
            key = request_key(url)
            if (
                not recheck_existing
                and not force
                and key in existing
                and "identity_error" not in existing[key].metadata
            ):
                continue
            try:
                body = fetch_bytes(
                    session,
                    url,
                    params=None,
                    max_bytes=_MAX_HTML_BYTES,
                    context=context,
                )
            except ProviderFetchStoppedError:
                raise
            except SourceWithheldError as error:
                raise ProviderFetchStoppedError(str(error)) from error
            except IndicatorsProviderError as error:
                failures.append(str(error))
                continue
            metadata: dict[str, object] = {
                "delivery_provider": "tradingeconomics",
                "measurement_provider": "spglobal",
                "stream": stream,
            }
            try:
                article = parse_article(body, stream=stream, url=url)
            except ValueError as error:
                # The raw reception remains available even when native identity fails.
                metadata["identity_error"] = str(error)
                prior = existing.get(key)
                if prior is not None:
                    requested = prior.metadata.get(
                        "requested_observed_at", prior.metadata.get("observed_at")
                    )
                    if requested is not None:
                        metadata["requested_observed_at"] = requested
                failures.append(f"{url}: {error}")
            else:
                if not start <= article.observed_at <= end and key not in existing:
                    continue
                prior = existing.get(key)
                if prior is not None:
                    requested = prior.metadata.get(
                        "requested_observed_at", prior.metadata.get("observed_at")
                    )
                    if requested is not None and requested != article.observed_at.isoformat():
                        metadata["requested_observed_at"] = requested
                metadata.update(
                    {
                        "observed_at": article.observed_at.isoformat(),
                        "phase": article.phase,
                        "published_at": article.published_at.isoformat(),
                        "modified_at": article.modified_at.isoformat(),
                    }
                )
            document = SourceDocument(
                source.source_id,
                key,
                url,
                "text/html",
                datetime.now(UTC),
                body,
                metadata,
            )
            latest[key] = document
            yield document
        covered = {
            str(document.metadata.get("observed_at"))
            for document in latest.values()
            if document.metadata.get("phase", "final") == "final"
        }
        first = start
        if first.day != 1:
            first = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        expected = end.replace(day=1)
        for _ in range(1 if end.day >= 10 else 2):
            expected = (expected - timedelta(days=1)).replace(day=1)
        while first <= expected:
            if first.isoformat() not in covered:
                failures.append(f"PMI final original is missing for {first.isoformat()}")
            first = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        if failures:
            raise IndicatorsProviderError(
                f"TE PMI incomplete: {len(failures)} article/discovery/coverage failures; "
                + "; ".join(failures[:3])
            )
