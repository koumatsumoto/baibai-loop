"""Locate JPX earnings source documents without building a projection."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from baibai_engine.data_collection.providers.jpx.models import JPXAcquisitionError, validate_jpx_url

EARNINGS_INDEX_URL = (
    "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
)
REGULATION_URLS = {
    "特別注意銘柄": "https://www.jpx.co.jp/listing/measures/alert/index.html",
    "整理銘柄": "https://www.jpx.co.jp/listing/market-alerts/supervision/",
    "取引停止": "https://www.jpx.co.jp/markets/equities/suspended/",
    "上場廃止警告": "https://www.jpx.co.jp/listing/stocks/delisted/",
}


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)


def earnings_urls(content: bytes, url: str, content_type: str = "") -> tuple[str, ...]:
    validate_jpx_url(url)
    charset = next(
        (
            p.split("=", 1)[1].strip().strip("\"'").lower()
            for p in content_type.split(";")
            if p.strip().lower().startswith("charset=")
        ),
        "",
    )
    charset = {"shift_jis": "cp932", "shift-jis": "cp932"}.get(charset, charset)
    for encoding in (charset, "utf-8-sig", "utf-8", "cp932"):
        if not encoding:
            continue
        try:
            html = content.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    else:
        raise JPXAcquisitionError("JPX index encoding is unsupported")
    links = _Links()
    links.feed(html)
    result: list[str] = []
    for href in links.hrefs:
        resolved = urljoin(url, href)
        parsed = urlparse(resolved)
        path = parsed.path.lower()
        if (
            parsed.scheme == "https"
            and parsed.netloc == "www.jpx.co.jp"
            and path.startswith("/listing/event-schedules/financial-announcement/")
            and Path(path).suffix in {".xls", ".xlsx"}
            and "kessan" in Path(path).name
            and resolved not in result
        ):
            result.append(resolved)
    if not result:
        raise JPXAcquisitionError("JPX earnings index has no workbook links")
    return tuple(result)
