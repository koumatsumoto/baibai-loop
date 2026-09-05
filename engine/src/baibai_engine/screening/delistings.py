"""Produce and read the JPX delisting fact index used by exit-value resolution."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import requests

from baibai_engine.market.sqlite import connect_current, open_connection
from baibai_engine.market.ticker import normalize_ticker

JPX_DELISTING_INDEX_URL = "https://www.jpx.co.jp/listing/stocks/delisted/"

_HTTP_TIMEOUT_SECONDS = 30
_JPX_ARCHIVE_RE = re.compile(r"/listing/stocks/delisted/(?:index\.html|archives-\d+\.html)$")


class DelistingSourceError(RuntimeError):
    """A JPX delisting source cannot be converted without guessing."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DelistingRecord:
    delisted_on: date
    ticker: str
    name: str
    market: str | None
    reason: str


def store_jpx_delistings(sqlite_path: Path, rows: Sequence[DelistingRecord]) -> int:
    """Accumulate immutable delisting facts as JPX retires archive pages."""
    if not rows:
        raise DelistingSourceError("refusing to store zero JPX delistings")
    connection = open_connection(sqlite_path)
    try:
        connection.executemany(
            "INSERT OR REPLACE INTO jpx_delistings(delisted_on, ticker, name, market, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (row.delisted_on.isoformat(), row.ticker, row.name, row.market, row.reason)
                for row in rows
            ),
        )
        connection.commit()
        return len(rows)
    finally:
        connection.close()


def read_jpx_delistings(sqlite_path: Path) -> tuple[DelistingRecord, ...]:
    connection = connect_current(sqlite_path)
    if connection is None:
        return ()
    try:
        rows = connection.execute(
            "SELECT delisted_on, ticker, name, market, reason FROM jpx_delistings "
            "ORDER BY delisted_on, ticker"
        ).fetchall()
    finally:
        connection.close()
    return tuple(
        DelistingRecord(
            delisted_on=date.fromisoformat(str(delisted_on)),
            ticker=str(ticker),
            name=str(name),
            market=None if market is None else str(market),
            reason=str(reason),
        )
        for delisted_on, ticker, name, market, reason in rows
    )


def download_jpx_delistings(
    session: requests.Session | None = None,
) -> tuple[DelistingRecord, ...]:
    """Download the current JPX page and same-origin archive pages."""
    client = session or requests.Session()
    index_response = client.get(JPX_DELISTING_INDEX_URL, timeout=_HTTP_TIMEOUT_SECONDS)
    if index_response.status_code >= 400:
        raise DelistingSourceError(
            f"failed to download JPX delistings: {index_response.status_code}"
        )
    index_parser = _TableLinkParser()
    index_parser.feed(_decode_jpx_html(index_response))
    urls = {JPX_DELISTING_INDEX_URL}
    for href in index_parser.links:
        resolved = urljoin(JPX_DELISTING_INDEX_URL, href)
        if resolved.startswith(JPX_DELISTING_INDEX_URL) and _JPX_ARCHIVE_RE.search(resolved):
            urls.add(resolved)
    rows: dict[tuple[date, str], DelistingRecord] = {}
    for url in sorted(urls):
        response = (
            index_response
            if url == JPX_DELISTING_INDEX_URL
            else client.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        )
        if response.status_code >= 400:
            raise DelistingSourceError(f"failed to download JPX delistings: {url}")
        parser = _TableLinkParser()
        parser.feed(_decode_jpx_html(response))
        for cells in parser.rows:
            row = _jpx_delisting_row(cells)
            if row is None:
                continue
            key = (row.delisted_on, row.ticker)
            existing = rows.get(key)
            if existing is not None and existing != row:
                raise DelistingSourceError(
                    f"conflicting JPX delisting row: {row.ticker} {row.delisted_on.isoformat()}"
                )
            rows[key] = row
    if not rows:
        raise DelistingSourceError("JPX delisting history had no typed rows")
    return tuple(rows[key] for key in sorted(rows))


class _TableLinkParser(HTMLParser):
    """Collect table rows and navigation targets from one JPX delisting page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, ...]] = []
        self.links: list[str] = []
        self._cells: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_by_name = dict(attrs)
        if tag == "a" and attrs_by_name.get("href"):
            self.links.append(str(attrs_by_name["href"]))
        if tag == "option" and attrs_by_name.get("value"):
            self.links.append(str(attrs_by_name["value"]))
        if tag == "tr":
            self._cells = []
        elif tag in {"th", "td"} and self._cells is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._cell is not None and self._cells is not None:
            self._cells.append(_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._cells is not None:
            if self._cells:
                self.rows.append(tuple(self._cells))
            self._cells = None


def _jpx_delisting_row(cells: Sequence[str]) -> DelistingRecord | None:
    """Read one table row, skipping headers and anything other than a delisting."""
    if len(cells) != 5:
        return None
    try:
        delisted_on = date.fromisoformat(cells[0].replace("/", "-"))
        ticker = normalize_ticker(cells[2])
    except ValueError:
        return None
    if not cells[1] or not cells[4]:
        return None
    return DelistingRecord(
        delisted_on=delisted_on,
        ticker=ticker,
        name=cells[1],
        market=cells[3] or None,
        reason=cells[4],
    )


def _decode_jpx_html(response: requests.Response) -> str:
    """Decode with strict codecs so a latin-1 fallback cannot produce mojibake."""
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return response.content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    raise DelistingSourceError("failed to decode JPX HTML")


def _text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


__all__ = (
    "DelistingRecord",
    "DelistingSourceError",
    "download_jpx_delistings",
    "read_jpx_delistings",
    "store_jpx_delistings",
)
