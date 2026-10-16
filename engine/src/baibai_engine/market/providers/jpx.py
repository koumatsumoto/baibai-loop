from __future__ import annotations

import csv
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import date
from html.parser import HTMLParser
from io import BytesIO
from itertools import groupby
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

from baibai_engine.market.jpx_models import (
    JPXEarningsCalendarEntry,
    JPXEarningsCalendarSnapshot,
    JPXProviderError,
    JPXRegulationSnapshot,
)
from baibai_engine.market.jpx_sources import JPX_SPECIAL_CAUTION_SOURCE_NAME
from baibai_engine.market.ticker import normalize_ticker

_JPX_ALLOWED_SCHEME = "https"
_JPX_ALLOWED_HOST = "www.jpx.co.jp"
_JPX_EXCEL_SUFFIXES = {".xls", ".xlsx"}
JPX_EARNINGS_CALENDAR_INDEX_URL = (
    "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
)
_JPX_EARNINGS_PATH_PREFIX = "/listing/event-schedules/financial-announcement/"
# Publication stamp carried by the archived cohort files, matched on
# whitespace-stripped cells.
_JPX_PUBLICATION_DATE_PATTERN = re.compile(
    r"(?:(\d{4})年(\d{1,2})月(\d{1,2})日現在|asof(\d{4})/(\d{1,2})/(\d{1,2}))",
    re.IGNORECASE,
)
# Cohort file precedence, most current view first.
_EARNINGS_RANK_ROLLING = 0
_EARNINGS_RANK_DATED = 1
_EARNINGS_RANK_UNDATED = 2
_EARNINGS_RANK_NO_FORWARD_VIEW = 3
_TRADING_HALT_EMPTY_MARKER = "現在、該当する情報はありません。"
_HTTP_TIMEOUT_SECONDS = 30
_LOGGER = logging.getLogger(__name__)
_MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class _EarningsCalendarFile:
    """One cohort Excel from the JPX financial-announcement index.

    `published_on` is the `YYYY年M月D日現在` stamp the archived cohort files
    carry, and is `None` both for the rolling daily file, which has no fixed
    as-of to stamp, and for an archive whose stamp could not be read. Telling
    those two apart is `_is_rolling_earnings_file`'s job, not this field's.
    """

    url: str
    published_on: date | None
    entries: tuple[JPXEarningsCalendarEntry, ...]
    raw_record_count: int
    excluded_record_count: int
    undated_tickers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class _ParsedRow:
    cells: tuple[str, ...]
    cell_tags: tuple[str, ...]
    rowspans: tuple[str, ...]
    colspans: tuple[str, ...]


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class _ParsedTable:
    classes: tuple[str, ...]
    rows: tuple[_ParsedRow, ...]
    preceding_h2: str | None
    preceding_h3: str | None
    header_th_texts: tuple[str, ...]


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class _ParsedLink:
    href: str
    text: str
    preceding_h2: str | None
    preceding_h3: str | None


class _JpxHtmlIndexer(HTMLParser):
    """Parse JPX HTML once and surface outer-most `<table>` blocks and links.

    Nested tables are dropped by construction (only tables closing at stack
    depth 0 are recorded), so callers can match by class / preceding heading /
    header text without writing brittle regex.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_ParsedTable] = []
        self.section_text: dict[str, list[str]] = {}
        self.links: list[_ParsedLink] = []
        self._table_stack: list[dict[str, object]] = []
        self._cell_fragments: list[str] | None = None
        self._cell_tag: str | None = None
        self._cell_rowspan = "1"
        self._cell_colspan = "1"
        self._link_href: str | None = None
        self._link_fragments: list[str] | None = None
        self._heading_buffer: list[str] | None = None
        self._heading_tag: str | None = None
        self._last_h2: str | None = None
        self._last_h3: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_by_name = dict(attrs)
        if tag == "a" and attrs_by_name.get("href"):
            self._link_href = attrs_by_name["href"]
            self._link_fragments = []
        if tag == "img" and self._link_fragments is not None:
            label = attrs_by_name.get("alt") or attrs_by_name.get("title")
            if label:
                self._link_fragments.append(label)
        if tag in {"h2", "h3"}:
            self._heading_buffer = []
            self._heading_tag = tag
            return
        if tag == "table":
            classes: tuple[str, ...] = ()
            for name, value in attrs:
                if name == "class" and value:
                    classes = tuple(value.split())
                    break
            self._table_stack.append(
                {
                    "classes": classes,
                    "rows": [],
                    "preceding_h2": self._last_h2,
                    "preceding_h3": self._last_h3,
                    "current_row": None,
                }
            )
            return
        if not self._table_stack:
            return
        if tag == "tr":
            self._table_stack[-1]["current_row"] = []
            return
        if tag in {"td", "th"}:
            self._cell_fragments = []
            self._cell_tag = tag
            self._cell_rowspan = attrs_by_name.get("rowspan") or "1"
            self._cell_colspan = attrs_by_name.get("colspan") or "1"
            return
        if tag == "br" and self._cell_fragments is not None:
            self._cell_fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._link_href is not None:
            value = "".join(self._link_fragments or []).replace("\xa0", " ")
            normalized = re.sub(r"\s+", " ", value).strip()
            self.links.append(
                _ParsedLink(
                    href=self._link_href,
                    text=normalized,
                    preceding_h2=self._last_h2,
                    preceding_h3=self._last_h3,
                )
            )
            self._link_href = None
            self._link_fragments = None
            return
        if tag in {"h2", "h3"} and self._heading_buffer is not None:
            text = re.sub(r"\s+", " ", "".join(self._heading_buffer)).strip()
            if self._heading_tag == "h2":
                self._last_h2 = text
                self._last_h3 = None
                self.section_text.setdefault(text, [])
            else:
                self._last_h3 = text
            self._heading_buffer = None
            self._heading_tag = None
            return
        if not self._table_stack:
            return
        if tag in {"td", "th"} and self._cell_fragments is not None:
            value = "".join(self._cell_fragments).replace("\xa0", " ")
            normalized = re.sub(r"\s+", " ", value).strip()
            current_row = self._table_stack[-1].get("current_row")
            if isinstance(current_row, list) and self._cell_tag is not None:
                current_row.append(
                    (self._cell_tag, normalized, self._cell_rowspan, self._cell_colspan)
                )
            self._cell_fragments = None
            self._cell_tag = None
            return
        if tag == "tr":
            current_row = self._table_stack[-1].get("current_row")
            self._table_stack[-1]["current_row"] = None
            if isinstance(current_row, list) and any(cell[1] for cell in current_row):
                current_table_rows = self._table_stack[-1]["rows"]
                if not isinstance(current_table_rows, list):
                    raise JPXProviderError("internal JPX parser row buffer is invalid")
                current_table_rows.append(
                    _ParsedRow(
                        cells=tuple(cell[1] for cell in current_row),
                        cell_tags=tuple(cell[0] for cell in current_row),
                        rowspans=tuple(cell[2] for cell in current_row),
                        colspans=tuple(cell[3] for cell in current_row),
                    )
                )
            return
        if tag == "table":
            data = self._table_stack.pop()
            rows_payload = data["rows"]
            if not isinstance(rows_payload, list):
                raise JPXProviderError("internal JPX parser row payload is invalid")
            parsed_rows: tuple[_ParsedRow, ...] = tuple(rows_payload)
            classes_payload = data["classes"]
            if not isinstance(classes_payload, tuple):
                raise JPXProviderError("internal JPX parser class payload is invalid")
            header_th_texts: tuple[str, ...] = ()
            for row in parsed_rows:
                if row.cell_tags and all(t == "th" for t in row.cell_tags):
                    header_th_texts = row.cells
                    break
            preceding_h2 = data["preceding_h2"]
            preceding_h3 = data["preceding_h3"]
            if preceding_h2 is not None and not isinstance(preceding_h2, str):
                raise JPXProviderError("internal JPX parser h2 payload is invalid")
            if preceding_h3 is not None and not isinstance(preceding_h3, str):
                raise JPXProviderError("internal JPX parser h3 payload is invalid")
            parsed = _ParsedTable(
                classes=classes_payload,
                rows=parsed_rows,
                preceding_h2=preceding_h2,
                preceding_h3=preceding_h3,
                header_th_texts=header_th_texts,
            )
            if not self._table_stack:
                self.tables.append(parsed)

    def handle_data(self, data: str) -> None:
        if self._last_h2 is not None and self._heading_buffer is None:
            self.section_text[self._last_h2].append(data)
        if self._heading_buffer is not None:
            self._heading_buffer.append(data)
        if self._cell_fragments is not None:
            self._cell_fragments.append(data)
        if self._link_fragments is not None:
            self._link_fragments.append(data)


class JPXProvider:
    """JPX public regulation provider with bounded HTML parsing for required sources."""

    def __init__(
        self,
        regulation_urls: Mapping[str, str] | None = None,
        session: requests.Session | None = None,
        *,
        sqlite_path: Path | None = None,
        cache_only: bool = False,
        allow_stale_snapshot: bool = False,
    ) -> None:
        self._session = session or requests.Session()
        self._regulation_urls = dict(regulation_urls or {})
        self._sqlite_path = Path(sqlite_path) if sqlite_path is not None else None
        self._cache_only = cache_only
        self._allow_stale_snapshot = allow_stale_snapshot

    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        if self._sqlite_path is not None:
            from baibai_engine.storage.observations.reader import read_jpx_regulations

            cached = read_jpx_regulations(self._sqlite_path, asof_date)
            if cached is not None:
                return cached
        self._raise_if_cache_only("jpx_regulation_flags", asof_date.isoformat())
        if not self._regulation_urls:
            raise JPXProviderError(
                "JPX regulation data is required but no public CSV/Excel/HTML URL is configured"
            )

        flags: dict[str, set[str]] = {}
        fetched_source_names: list[str] = []
        for source_name, url in self._regulation_urls.items():
            rows = self._download_rows(source_name, url)
            fetched_source_names.append(source_name)
            for row in rows:
                ticker_raw = (
                    row.get("ticker")
                    or row.get("code")
                    or row.get("銘柄コード")
                    or row.get("コード")
                )
                flag = row.get("flag") or row.get("規制区分") or row.get("status") or source_name
                if not ticker_raw:
                    raise JPXProviderError(
                        f"missing JPX code column in regulation source {source_name}"
                    )
                ticker = parse_jpx_code(ticker_raw)
                flags.setdefault(ticker, set()).add(str(flag))

        snapshot = JPXRegulationSnapshot(
            flags_by_ticker={
                ticker: tuple(sorted(values)) for ticker, values in sorted(flags.items())
            },
            source_names=tuple(sorted(fetched_source_names)),
        )
        if self._sqlite_path is not None:
            from baibai_engine.storage.observations.ingest.jpx import store_jpx_regulations

            store_jpx_regulations(
                self._sqlite_path,
                asof_date,
                flags_by_ticker=snapshot.flags_by_ticker,
                source_names=snapshot.source_names,
            )
        return snapshot

    def get_earnings_calendar_snapshot(self, asof_date: date) -> JPXEarningsCalendarSnapshot:
        if self._sqlite_path is not None:
            from baibai_engine.storage.observations.reader import (
                read_jpx_earnings_calendar_snapshot,
            )

            cached = read_jpx_earnings_calendar_snapshot(
                self._sqlite_path,
                asof_date,
                allow_stale=self._allow_stale_snapshot,
            )
            if cached is not None:
                return cached
        self._raise_if_cache_only("jpx_earnings_calendar", asof_date.isoformat())
        source_urls = self._resolve_earnings_calendar_urls()
        files = [self._download_earnings_calendar(url) for url in source_urls]
        raw_count = sum(source.raw_record_count for source in files)
        excluded_count = sum(source.excluded_record_count for source in files)
        _warn_on_unexpected_earnings_sources(files)
        entries: dict[str, date | None] = {}
        superseded: list[str] = []
        conflicts: list[str] = []
        ordered = sorted(files, key=lambda source: _earnings_source_precedence(source, asof_date))
        # URL orders traversal only. Equally current sources must agree on a date;
        # explicit unknowns and conflicts block fallback to an older file.
        for _, sources in groupby(
            ordered, key=lambda source: _earnings_source_precedence(source, asof_date)[:2]
        ):
            candidates: dict[str, list[date | None]] = {}
            for source in sources:
                for ticker in source.undated_tickers:
                    candidates.setdefault(ticker, []).append(None)
                for entry in source.entries:
                    candidates.setdefault(entry.ticker, []).append(entry.announcement_date)
            for ticker, dates in candidates.items():
                if ticker in entries:
                    superseded.extend(
                        f"{ticker}: kept {entries[ticker]}, dropped {on_date}"
                        for on_date in dates
                        if on_date is not None and on_date != entries[ticker]
                    )
                    continue
                distinct = set(dates)
                entries[ticker] = next(iter(distinct)) if len(distinct) == 1 else None
                if len(distinct) > 1:
                    conflicts.append(ticker)
        if conflicts:
            _LOGGER.warning(
                "JPX earnings calendar has equally current conflicting schedules: %s",
                ", ".join(sorted(conflicts)),
            )
        if superseded:
            _LOGGER.warning(
                "JPX earnings calendar superseded %d lower-precedence date(s): %s",
                len(superseded),
                "; ".join(superseded[:20]),
            )
        normalized_entries = tuple(
            JPXEarningsCalendarEntry(ticker=ticker, announcement_date=on_date)
            for ticker, on_date in sorted(
                ((ticker, day) for ticker, day in entries.items() if day is not None),
                key=lambda item: (item[1], item[0]),
            )
        )
        if not normalized_entries:
            raise JPXProviderError("JPX earnings calendar snapshot has zero valid rows")
        if max(entry.announcement_date for entry in normalized_entries) < asof_date:
            raise JPXProviderError("JPX earnings calendar snapshot contains only past dates")
        snapshot = JPXEarningsCalendarSnapshot(
            entries=normalized_entries,
            source_urls=source_urls,
            raw_record_count=raw_count,
            excluded_record_count=excluded_count,
            superseded_record_count=len(superseded),
        )
        if self._sqlite_path is not None:
            from baibai_engine.storage.observations.ingest.jpx import (
                store_jpx_earnings_calendar_snapshot,
            )

            store_jpx_earnings_calendar_snapshot(self._sqlite_path, snapshot)
        return snapshot


    def _raise_if_cache_only(self, source: str, requirement: str) -> None:
        if not self._cache_only:
            return
        sqlite_label = self._sqlite_path.as_posix() if self._sqlite_path is not None else "<none>"
        raise JPXProviderError(
            f"SQLite cache incomplete for {source} ({requirement}); "
            f"sqlite={sqlite_label}. `screening run` is cache-only: bootstrap or repair "
            "SQLite before running screening."
        )

    def bootstrap_cache(self, asof_date: date) -> dict[str, int]:
        earnings = self.get_earnings_calendar_snapshot(asof_date)
        snapshot = self.get_regulation_snapshot(asof_date)
        counts = {
            "earnings_calendar_rows": earnings.valid_record_count,
            "regulated_tickers": len(snapshot.flags_by_ticker),
        }
        # A cached read did no merge, so printing a zero here would read as
        # "nothing was superseded today" when nothing was measured.
        if earnings.superseded_record_count is not None:
            counts["earnings_calendar_superseded"] = earnings.superseded_record_count
        return counts

    def _resolve_earnings_calendar_urls(self) -> tuple[str, ...]:
        index_url = JPX_EARNINGS_CALENDAR_INDEX_URL
        self._validate_allowed_url(index_url)
        try:
            response = self._session.get(index_url, timeout=_HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise JPXProviderError(
                f"failed to download JPX earnings calendar index: {index_url}"
            ) from exc
        if response.status_code >= 400:
            raise JPXProviderError(
                f"failed to download JPX earnings calendar index: {index_url} "
                f"(status={response.status_code})"
            )
        html = self._decode_html_text(
            response.content, index_url, response.headers.get("content-type", "")
        )
        indexer = _JpxHtmlIndexer()
        indexer.feed(html)
        urls: list[str] = []
        for link in indexer.links:
            resolved = urljoin(index_url, link.href)
            parsed = urlparse(resolved)
            path_lower = parsed.path.lower()
            if parsed.scheme != _JPX_ALLOWED_SCHEME or parsed.netloc != _JPX_ALLOWED_HOST:
                continue
            if not path_lower.startswith(_JPX_EARNINGS_PATH_PREFIX):
                continue
            if Path(path_lower).suffix not in _JPX_EXCEL_SUFFIXES:
                continue
            if "kessan" not in Path(path_lower).name:
                continue
            if resolved not in urls:
                urls.append(resolved)
        if not urls:
            raise JPXProviderError(
                f"failed to locate JPX earnings calendar Excel links: {index_url}"
            )
        return tuple(urls)

    def _download_earnings_calendar(self, url: str) -> _EarningsCalendarFile:
        self._validate_allowed_url(url)
        parsed_url = urlparse(url)
        if not parsed_url.path.lower().startswith(_JPX_EARNINGS_PATH_PREFIX):
            raise JPXProviderError(f"JPX earnings calendar URL is outside allowed path: {url}")
        try:
            response = self._session.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            raise JPXProviderError(f"failed to download JPX earnings calendar: {url}") from exc
        if response.status_code >= 400:
            raise JPXProviderError(
                f"failed to download JPX earnings calendar: {url} (status={response.status_code})"
            )
        return self._parse_earnings_calendar_excel(response.content, url)

    def _parse_earnings_calendar_excel(self, content: bytes, url: str) -> _EarningsCalendarFile:
        try:
            import pandas as pd
        except ModuleNotFoundError as exc:
            raise JPXProviderError("pandas is required to read JPX Excel sources") from exc
        try:
            frame = pd.read_excel(BytesIO(content), header=None, dtype=str).fillna("")
        except Exception as exc:
            raise JPXProviderError(f"failed to parse JPX earnings calendar Excel: {url}") from exc
        header_index: int | None = None
        code_column: int | None = None
        date_column: int | None = None
        published_on: date | None = None
        for row_index, (_, raw_row) in enumerate(frame.iterrows()):
            cells = [re.sub(r"\s+", "", str(value)) for value in raw_row.tolist()]
            if published_on is None:
                published_on = _parse_jpx_publication_date(cells)
            code_candidates = [
                index
                for index, value in enumerate(cells)
                if value.startswith(("コード", "銘柄コード", "証券コード"))
            ]
            date_candidates = [
                index
                for index, value in enumerate(cells)
                if "決算" in value and "発表" in value and "日" in value
            ]
            if code_candidates and date_candidates:
                header_index = row_index
                code_column = code_candidates[0]
                date_column = date_candidates[0]
                break
        if header_index is None or code_column is None or date_column is None:
            raise JPXProviderError(f"unexpected JPX earnings calendar header layout: {url}")
        dates_by_ticker: dict[str, set[date | None]] = {}
        counts_by_ticker: dict[str, int] = {}
        unparsable: list[str] = []
        for _, raw_row in frame.iloc[header_index + 1 :].iterrows():
            values = [str(value).strip() for value in raw_row.tolist()]
            code_raw = values[code_column] if code_column < len(values) else ""
            date_raw = values[date_column] if date_column < len(values) else ""
            if not code_raw:
                continue
            if not date_raw and re.match(r"^(?:※|注|備考|note\b)", code_raw, re.IGNORECASE):
                continue
            ticker = parse_jpx_code(code_raw)
            announcement_date = None
            if date_raw and not date_raw.startswith("未定"):
                announcement_date = _parse_jpx_earnings_date(date_raw)
                if announcement_date is None:
                    unparsable.append(f"{ticker}: {date_raw!r}")
            dates_by_ticker.setdefault(ticker, set()).add(announcement_date)
            counts_by_ticker[ticker] = counts_by_ticker.get(ticker, 0) + 1

        entries: dict[str, JPXEarningsCalendarEntry] = {}
        undated: set[str] = set()
        conflicts: list[str] = []
        for ticker, dates in dates_by_ticker.items():
            if len(dates) == 1 and None not in dates:
                on_date = next(iter(dates))
                assert on_date is not None
                entries[ticker] = JPXEarningsCalendarEntry(ticker=ticker, announcement_date=on_date)
            else:
                undated.add(ticker)
                if len(dates) > 1:
                    rendered = sorted(str(day) if day is not None else "undated" for day in dates)
                    conflicts.append(f"{ticker}: " + " and ".join(rendered))
        raw_count = sum(counts_by_ticker.values())
        excluded_count = sum(counts_by_ticker[ticker] for ticker in undated)
        if conflicts or unparsable:
            _LOGGER.warning(
                "JPX earnings calendar dropped undated ticker(s) in %s: "
                "conflicting=%d unparsable=%d: %s",
                url,
                len(conflicts),
                len(unparsable),
                "; ".join((conflicts + unparsable)[:20]),
            )
        return _EarningsCalendarFile(
            url=url,
            published_on=published_on,
            entries=tuple(entries.values()),
            raw_record_count=raw_count,
            excluded_record_count=excluded_count,
            undated_tickers=tuple(sorted(undated)),
        )

    def _download_rows(self, source_name: str, url: str) -> list[dict[str, str]]:
        self._validate_allowed_url(url)
        response = self._session.get(url, timeout=_HTTP_TIMEOUT_SECONDS)
        if response.status_code >= 400:
            raise JPXProviderError(
                f"failed to download JPX regulation source: {url} (status={response.status_code})"
            )
        suffix = Path(urlparse(url).path).suffix.lower()
        content_type_header = response.headers.get("content-type", "")
        content_type = content_type_header.split(";", 1)[0].strip().lower()
        if suffix == ".csv":
            return self._parse_csv_rows(response.content, url)
        if suffix in {".xls", ".xlsx"}:
            return self._parse_excel_rows(response.content, url)
        if suffix == ".html" or content_type == "text/html":
            return self._parse_html_rows(source_name, response.content, url, content_type_header)
        raise JPXProviderError(f"unsupported JPX regulation source format: {url}")

    def _validate_allowed_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != _JPX_ALLOWED_SCHEME or parsed.netloc != _JPX_ALLOWED_HOST:
            raise JPXProviderError(f"JPX regulation source must use https://www.jpx.co.jp/: {url}")

    def _parse_csv_rows(self, content: bytes, url: str) -> list[dict[str, str]]:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = content.decode("cp932")
            except UnicodeDecodeError as exc:
                raise JPXProviderError(f"failed to decode JPX regulation source: {url}") from exc
        reader = csv.DictReader(text.splitlines())
        return [dict(row) for row in reader]

    def _parse_excel_rows(self, content: bytes, url: str) -> list[dict[str, str]]:
        try:
            import pandas as pd
        except ModuleNotFoundError as exc:
            raise JPXProviderError("pandas is required to read JPX Excel sources") from exc

        try:
            frame = pd.read_excel(BytesIO(content), dtype=str)
        except ImportError as exc:
            raise JPXProviderError(
                f"missing Excel reader dependency for JPX source: {url}"
            ) from exc
        except Exception as exc:
            raise JPXProviderError(f"failed to parse JPX Excel source: {url}") from exc

        normalized = frame.fillna("")
        return [
            {str(column): str(value).strip() for column, value in row.items()}
            for row in normalized.to_dict(orient="records")
        ]

    def _parse_html_rows(
        self,
        source_name: str,
        content: bytes,
        url: str,
        content_type_header: str = "",
    ) -> list[dict[str, str]]:
        html = self._decode_html_text(content, url, content_type_header)
        if source_name == JPX_SPECIAL_CAUTION_SOURCE_NAME:
            return self._parse_special_attention_html_rows(html, url)
        if source_name == "整理銘柄":
            return self._parse_reorganization_html_rows(html, url)
        if source_name == "取引停止":
            return self._parse_trading_halt_html_rows(html, url)
        if source_name == "上場廃止警告":
            return self._parse_delisting_warning_html_rows(html, url)
        raise JPXProviderError(f"unsupported JPX HTML parser source: {source_name}")

    def _decode_html_text(self, content: bytes, url: str, content_type_header: str = "") -> str:
        # Prefer the charset advertised by the server, fall back to common JPX encodings.
        # Shift_JIS is normalized to cp932 (its lossless superset).
        encodings: list[str] = []
        charset = self._extract_charset(content_type_header)
        if charset:
            normalized = {"shift_jis": "cp932", "shift-jis": "cp932"}.get(charset, charset)
            encodings.append(normalized)
        encodings.extend(("utf-8-sig", "utf-8", "cp932"))
        seen: set[str] = set()
        for encoding in encodings:
            if encoding in seen:
                continue
            seen.add(encoding)
            try:
                return content.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        raise JPXProviderError(f"failed to decode JPX HTML source: {url}")

    @staticmethod
    def _extract_charset(content_type_header: str) -> str | None:
        for part in content_type_header.split(";"):
            kv = part.strip()
            if kv.lower().startswith("charset="):
                value = kv.split("=", 1)[1].strip().strip("\"'").lower()
                return value or None
        return None

    def _parse_special_attention_html_rows(self, html: str, url: str) -> list[dict[str, str]]:
        indexer = _JpxHtmlIndexer()
        indexer.feed(html)
        sections = [title for title in indexer.section_text if title.startswith("現在の指定状況")]
        error = f"unexpected JPX special caution HTML layout: {url}"
        if not sections:
            raise JPXProviderError(error)
        codes: dict[str, str] = {}
        for section in sections:
            tables = [table for table in indexer.tables if table.preceding_h2 == section]
            if not tables:
                if _TRADING_HALT_EMPTY_MARKER in "".join(indexer.section_text[section]):
                    continue
                raise JPXProviderError(error)
            if len(tables) != 1:
                raise JPXProviderError(error)
            table = tables[0]
            header = table.header_th_texts
            if header.count("コード") != 1 or not table.rows or table.rows[0].cells != header:
                raise JPXProviderError(error)
            code_column = header.index("コード")
            # JPX uses paired rows: the code spans both, while designation and
            # continuation dates occupy separate rows. Only consume the code row;
            # validate the exact remaining cells instead of skipping short rows.
            continuation_columns: tuple[int, ...] | None = None
            data_count = 0
            for row_index, row in enumerate(table.rows):
                if continuation_columns is not None:
                    expected_tag = "th" if row_index == 1 else "td"
                    if (
                        len(row.cells) != len(continuation_columns)
                        or any(tag != expected_tag for tag in row.cell_tags)
                        or any(span != "1" for span in row.rowspans + row.colspans)
                    ):
                        raise JPXProviderError(error)
                    continuation_columns = None
                    continue
                expected_tag = "th" if row_index == 0 else "td"
                if (
                    len(row.cells) != len(header)
                    or any(tag != expected_tag for tag in row.cell_tags)
                    or any(span not in {"1", "2"} for span in row.rowspans)
                    or any(span != "1" for span in row.colspans)
                ):
                    raise JPXProviderError(error)
                if "2" in row.rowspans:
                    if row.rowspans[code_column] != "2":
                        raise JPXProviderError(error)
                    continuation_columns = tuple(
                        index for index, span in enumerate(row.rowspans) if span == "1"
                    )
                if row_index:
                    raw_code = row.cells[code_column]
                    codes.setdefault(parse_jpx_code(raw_code), raw_code)
                    data_count += 1
            if continuation_columns is not None or not data_count:
                raise JPXProviderError(error)
        return [
            {"code": raw_code, "flag": JPX_SPECIAL_CAUTION_SOURCE_NAME}
            for raw_code in codes.values()
        ]

    def _parse_reorganization_html_rows(self, html: str, url: str) -> list[dict[str, str]]:
        indexer = _JpxHtmlIndexer()
        indexer.feed(html)
        error_label = "h3>整理銘柄 + table.fixedhead"
        table = self._select_table(
            indexer,
            url=url,
            error_label=error_label,
            table_classes=("fixedhead",),
            preceding_h3="整理銘柄",
        )
        return self._rows_from_parsed_table(
            table,
            source_name="整理銘柄",
            url=url,
            header_rows=1,
            code_column=2,
            expected_header="コード",
            error_label=error_label,
        )

    def _parse_trading_halt_html_rows(self, html: str, url: str) -> list[dict[str, str]]:
        indexer = _JpxHtmlIndexer()
        indexer.feed(html)
        error_label = "h2>本日の売買停止情報 + table.fixedhead.rows-2"
        try:
            table = self._select_table(
                indexer,
                url=url,
                error_label=error_label,
                table_classes=("fixedhead", "rows-2"),
                preceding_h2="本日の売買停止情報",
            )
        except JPXProviderError:
            section = indexer.section_text.get("本日の売買停止情報", [])
            if _TRADING_HALT_EMPTY_MARKER in "".join(section):
                return []
            raise
        return self._rows_from_parsed_table(
            table,
            source_name="取引停止",
            url=url,
            header_rows=2,
            code_column=1,
            expected_header="コード",
            error_label=error_label,
        )

    def _parse_delisting_warning_html_rows(self, html: str, url: str) -> list[dict[str, str]]:
        indexer = _JpxHtmlIndexer()
        indexer.feed(html)
        error_label = "table.fixedhead th=上場廃止日"
        table = self._select_table(
            indexer,
            url=url,
            error_label=error_label,
            table_classes=("fixedhead",),
            header_text="上場廃止日",
        )
        return self._rows_from_parsed_table(
            table,
            source_name="上場廃止警告",
            url=url,
            header_rows=1,
            code_column=2,
            expected_header="コード",
            error_label=error_label,
        )

    def _select_table(
        self,
        indexer: _JpxHtmlIndexer,
        *,
        url: str,
        error_label: str,
        table_classes: tuple[str, ...],
        preceding_h2: str | None = None,
        preceding_h3: str | None = None,
        header_text: str | None = None,
    ) -> _ParsedTable:
        for table in indexer.tables:
            if not all(cls in table.classes for cls in table_classes):
                continue
            if preceding_h2 is not None and table.preceding_h2 != preceding_h2:
                continue
            if preceding_h3 is not None and table.preceding_h3 != preceding_h3:
                continue
            if header_text is not None and header_text not in table.header_th_texts:
                continue
            return table
        raise JPXProviderError(f"failed to locate JPX HTML table for {error_label}: {url}")

    def _rows_from_parsed_table(
        self,
        table: _ParsedTable,
        *,
        source_name: str,
        url: str,
        header_rows: int,
        code_column: int,
        expected_header: str,
        error_label: str,
    ) -> list[dict[str, str]]:
        rows = table.rows
        if len(rows) < header_rows + 1:
            raise JPXProviderError(f"failed to parse JPX HTML table rows for {error_label}: {url}")
        # All header rows must be entirely <th> cells. This anchors against
        # silent layout drift such as JPX dropping rowspan and serving a single
        # header row, which would otherwise let a <td> data row slip into the
        # header and shrink the parsed payload by one record.
        for row in rows[:header_rows]:
            if not row.cell_tags or not all(tag == "th" for tag in row.cell_tags):
                raise JPXProviderError(
                    f"unexpected JPX HTML header layout for {error_label}: {url}"
                )
        header = rows[0]
        if len(header.cells) <= code_column or header.cells[code_column] != expected_header:
            raise JPXProviderError(f"unexpected JPX HTML table layout for {error_label}: {url}")

        parsed_rows: list[dict[str, str]] = []
        for row in rows[header_rows:]:
            if len(row.cells) <= code_column:
                raise JPXProviderError(f"unexpected JPX HTML row layout for {error_label}: {url}")
            if row.cell_tags and all(tag == "th" for tag in row.cell_tags):
                raise JPXProviderError(
                    f"unexpected JPX HTML data row layout for {error_label}: {url}"
                )
            code = row.cells[code_column]
            if not code:
                raise JPXProviderError(f"missing JPX code in HTML row for {error_label}: {url}")
            parsed_rows.append({"code": code, "flag": source_name})
        return parsed_rows


def parse_jpx_code(code: object) -> str:
    raw = str(code or "").strip()
    if len(raw) == 5 and raw.endswith("0"):
        raw = raw[:4]
    try:
        return normalize_ticker(raw)
    except ValueError as exc:
        raise JPXProviderError(f"invalid JPX code: {code!r}") from exc


def _parse_jpx_publication_date(cells: Sequence[str]) -> date | None:
    """Read the `YYYY年M月D日現在` / `As of YYYY/M/D` stamp from a pre-header row.

    Matching is anchored on the stamp wording rather than on any date-shaped
    text so cohort titles such as `2027年3月期第1四半期決算` are never mistaken
    for a publication date. An unrecognised stamp yields `None`, which leaves the
    file undatable and therefore unable to outrank a dated one.
    """
    for value in cells:
        match = _JPX_PUBLICATION_DATE_PATTERN.search(value)
        if match is None:
            continue
        try:
            year, month, day = (int(part) for part in match.groups() if part is not None)
            return date(year, month, day)
        except ValueError:
            continue
    return None


def _is_rolling_earnings_file(url: str) -> bool:
    """Tell the daily file from the dated cohort archives by its stable name.

    JPX publishes the next business day's disclosures as `kessan.xlsx` and the
    per-cohort archives as `kessanMM_MMDD.xlsx`. Identity comes from the name
    rather than from a missing publication stamp so that an archive whose stamp
    stops parsing cannot promote itself to "current".
    """
    return Path(urlparse(url).path).stem.lower() == "kessan"


def _earnings_source_precedence(
    source: _EarningsCalendarFile, asof_date: date
) -> tuple[int, int, str]:
    """Order cohort files by how current their view of the schedule is.

    A file with only past dates and no explicit unknowns is demoted. Unknown
    schedules must retain their source precedence so older dates cannot replace
    them. Publication order is kept inside each rank. URL gives stable traversal
    only; equally ranked conflicting schedules remain unresolved during merge.
    This is the adapter's policy, not an official JPX conflict-resolution rule.
    """
    published_rank = 0 if source.published_on is None else -source.published_on.toordinal()
    if not source.undated_tickers and not any(
        entry.announcement_date >= asof_date for entry in source.entries
    ):
        return (_EARNINGS_RANK_NO_FORWARD_VIEW, published_rank, source.url)
    if _is_rolling_earnings_file(source.url):
        return (_EARNINGS_RANK_ROLLING, 0, source.url)
    if source.published_on is None:
        return (_EARNINGS_RANK_UNDATED, 0, source.url)
    return (_EARNINGS_RANK_DATED, published_rank, source.url)


def _warn_on_unexpected_earnings_sources(files: Sequence[_EarningsCalendarFile]) -> None:
    """Say when the JPX layout stopped matching what precedence relies on."""
    rolling = [source.url for source in files if _is_rolling_earnings_file(source.url)]
    # JPX omits the daily workbook when no next-business-day disclosure is listed.
    # Its absence alone is not a layout error; monthly files can still supply dates.
    if len(rolling) > 1:
        _LOGGER.warning(
            "JPX earnings calendar index lists several rolling `kessan` files, "
            "so which one is current cannot be told apart: %s",
            ", ".join(rolling),
        )
    undated = [
        source.url
        for source in files
        if source.published_on is None and not _is_rolling_earnings_file(source.url)
    ]
    if undated:
        _LOGGER.warning(
            "JPX earnings cohort file has no readable publication stamp and "
            "cannot outrank a dated one: %s",
            ", ".join(undated),
        )


def _parse_jpx_earnings_date(raw: str) -> date | None:
    normalized = raw.strip().replace("年", "-").replace("月", "-").replace("日", "")
    normalized = normalized.replace("/", "-").replace(".", "-")[:10]
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None
