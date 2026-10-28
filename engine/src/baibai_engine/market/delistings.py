"""Pure JPX delisting page decoding and the stored delisting observation view."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.storage.observations.schema import connect_current, open_connection

JPX_DELISTING_INDEX_URL = "https://www.jpx.co.jp/listing/stocks/delisted/"
_JPX_ARCHIVE_RE = re.compile(r"/listing/stocks/delisted/archives-\d+\.html$")
_DATE_SHAPE_RE = re.compile(r"^\d{4}(?:[/-]\d{1,2}[/-]\d{1,2}|年\d{1,2}月\d{1,2}日)$")
_HEADER = ("上場廃止日", "銘柄名", "コード", "市場区分", "上場廃止理由")


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
    """Atomically replace the observed view from one complete source bundle."""
    if not rows:
        raise DelistingSourceError("refusing to store zero JPX delistings")
    connection = open_connection(sqlite_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DELETE FROM jpx_delistings")
        connection.executemany(
            "INSERT INTO jpx_delistings(delisted_on, ticker, name, market, reason) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (row.delisted_on.isoformat(), row.ticker, row.name, row.market, row.reason)
                for row in rows
            ),
        )
        connection.commit()
        return len(rows)
    except BaseException:
        connection.rollback()
        raise
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


def resolve_delisting_archive_urls(content: bytes, index_url: str) -> tuple[str, ...]:
    """Use only archive links present in this retained index, in stable URL order."""
    _validate_page_url(index_url, index=True)
    parser = _TableLinkParser()
    parser.feed(_decode_jpx_html(content))
    urls: set[str] = set()
    for href in parser.links:
        resolved = urljoin(index_url, href)
        if _is_archive_url(resolved):
            urls.add(resolved)
    return tuple(sorted(urls))


def parse_delisting_page(content: bytes, url: str) -> tuple[DelistingRecord, ...]:
    """Reject error pages and malformed dated rows instead of treating them as absence."""
    _validate_page_url(url)
    parser = _TableLinkParser()
    parser.feed(_decode_jpx_html(content))
    found_target = False
    records: list[DelistingRecord] = []
    for table in parser.tables:
        target_table = any(tuple(cells[:5]) == _HEADER for cells in table)
        for cells in table:
            if tuple(cells[:5]) == _HEADER:
                found_target = True
                continue
            if not cells or not (
                (target_table and len(cells) == 5) or _DATE_SHAPE_RE.fullmatch(cells[0])
            ):
                continue
            row = _jpx_delisting_row(cells, url=url)
            found_target = True
            records.append(row)
    if not found_target:
        raise DelistingSourceError(f"JPX delisting page has no recognizable table: {url}")
    return tuple(records)


def build_delisting_snapshot(pages: Sequence[tuple[str, bytes]]) -> tuple[DelistingRecord, ...]:
    """Parse every selected page before the caller can replace L2."""
    rows: dict[tuple[date, str], DelistingRecord] = {}
    for url, body in pages:
        for row in parse_delisting_page(body, url):
            key = (row.delisted_on, row.ticker)
            existing = rows.get(key)
            if existing is not None and existing != row:
                raise DelistingSourceError(
                    f"conflicting JPX delisting row: {row.ticker} {row.delisted_on.isoformat()}"
                )
            rows[key] = row
    if not rows:
        raise DelistingSourceError("JPX delisting pages had no typed rows")
    return tuple(rows[key] for key in sorted(rows))


class _TableLinkParser(HTMLParser):
    """Capture outer tables and navigation targets without network access."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[tuple[tuple[str, ...], ...]] = []
        self.links: list[str] = []
        self._depth = 0
        self._rows: list[tuple[str, ...]] | None = None
        self._cells: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_by_name = dict(attrs)
        if tag == "a" and attrs_by_name.get("href"):
            self.links.append(str(attrs_by_name["href"]))
        if tag == "option" and attrs_by_name.get("value"):
            self.links.append(str(attrs_by_name["value"]))
        if tag == "table":
            if self._depth == 0:
                self._rows = []
            self._depth += 1
        elif tag == "tr" and self._depth == 1:
            self._cells = []
        elif tag in {"th", "td"} and self._cells is not None and self._depth == 1:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if (
            tag in {"th", "td"}
            and self._depth == 1
            and self._cell is not None
            and self._cells is not None
        ):
            self._cells.append(_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._depth == 1 and self._cells is not None:
            if self._cells and self._rows is not None:
                self._rows.append(tuple(self._cells))
            self._cells = None
        elif tag == "table" and self._depth:
            self._depth -= 1
            if self._depth == 0 and self._rows is not None:
                self.tables.append(tuple(self._rows))
                self._rows = None


def _jpx_delisting_row(cells: Sequence[str], *, url: str) -> DelistingRecord:
    if len(cells) != 5 or not cells[1] or not cells[4]:
        raise DelistingSourceError(f"incomplete JPX delisting row: {url}")
    try:
        delisted_on = date.fromisoformat(cells[0].replace("/", "-"))
        ticker = normalize_ticker(cells[2])
    except ValueError as exc:
        raise DelistingSourceError(f"invalid JPX delisting date or code: {url}") from exc
    return DelistingRecord(
        delisted_on=delisted_on,
        ticker=ticker,
        name=cells[1],
        market=cells[3] or None,
        reason=cells[4],
    )


def _validate_page_url(url: str, *, index: bool = False) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.jpx.co.jp"
        or parsed.query
        or parsed.fragment
    ):
        raise DelistingSourceError(f"JPX delisting URL must use the allowed origin: {url}")
    if parsed.path not in (
        "/listing/stocks/delisted/",
        "/listing/stocks/delisted/index.html",
    ) and not _JPX_ARCHIVE_RE.fullmatch(parsed.path):
        raise DelistingSourceError(f"JPX delisting URL is outside the allowed archive: {url}")
    if index and parsed.path not in (
        "/listing/stocks/delisted/",
        "/listing/stocks/delisted/index.html",
    ):
        raise DelistingSourceError(f"JPX delisting index URL is invalid: {url}")


def _is_archive_url(url: str) -> bool:
    try:
        _validate_page_url(url)
    except DelistingSourceError:
        return False
    return bool(_JPX_ARCHIVE_RE.fullmatch(urlparse(url).path))


def _decode_jpx_html(content: bytes) -> str:
    """Decode with strict codecs so a latin-1 fallback cannot produce mojibake."""
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    raise DelistingSourceError("failed to decode JPX HTML")


def _text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


__all__ = (
    "DelistingRecord",
    "DelistingSourceError",
    "build_delisting_snapshot",
    "parse_delisting_page",
    "read_jpx_delistings",
    "resolve_delisting_archive_urls",
    "store_jpx_delistings",
)
