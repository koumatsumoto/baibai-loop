from __future__ import annotations

import csv
import logging
import re
from collections.abc import Mapping
from datetime import date, datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass

from baibai_loop.foundation.date_utils import weekday_distance
from baibai_loop.foundation.time import JST

from ..jpx_sources import JPX_SPECIAL_CAUTION_SOURCE_NAME
from ..schema import normalize_ticker

_JPX_ALLOWED_SCHEME = "https"
_JPX_ALLOWED_HOST = "www.jpx.co.jp"
_JPX_CACHE_SCHEMA_VERSION = 1
_JPX_STALE_SNAPSHOT_BUSINESS_DAYS = 7
# Current JPX margin Excel URL pattern. Keep the broader text/heading anchors
# below so a future filename prefix change fails less often and never silently.
_JPX_SPECIAL_CAUTION_MARGIN_LINK_HINT = "mtdailyk"
_JPX_EXCEL_SUFFIXES = {".xls", ".xlsx"}
JPX_EARNINGS_CALENDAR_INDEX_URL = (
    "https://www.jpx.co.jp/listing/event-schedules/financial-announcement/index.html"
)
_JPX_EARNINGS_PATH_PREFIX = "/listing/event-schedules/financial-announcement/"
_TRADING_HALT_EMPTY_MARKER = "現在、該当する情報はありません。"
_HTTP_TIMEOUT_SECONDS = 30
_LOGGER = logging.getLogger(__name__)
_MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False)


class JPXProviderError(RuntimeError):
    """Raised when required JPX public CSV/Excel/HTML sources are unavailable."""


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class JPXRegulationSnapshot:
    flags_by_ticker: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)
    source_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class JPXEarningsCalendarEntry:
    ticker: str
    announcement_date: date


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class JPXEarningsCalendarSnapshot:
    entries: tuple[JPXEarningsCalendarEntry, ...]
    source_urls: tuple[str, ...]
    raw_record_count: int
    excluded_record_count: int
    rejected_record_count: int

    @property
    def valid_record_count(self) -> int:
        return len(self.entries)

    @property
    def min_date(self) -> date:
        return min(entry.announcement_date for entry in self.entries)

    @property
    def max_date(self) -> date:
        return max(entry.announcement_date for entry in self.entries)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class _ParsedRow:
    cells: tuple[str, ...]
    cell_tags: tuple[str, ...]


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
        self.links: list[_ParsedLink] = []
        self._table_stack: list[dict[str, object]] = []
        self._cell_fragments: list[str] | None = None
        self._cell_tag: str | None = None
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
                current_row.append((self._cell_tag, normalized))
            self._cell_fragments = None
            self._cell_tag = None
            return
        if tag == "tr":
            current_row = self._table_stack[-1].get("current_row")
            self._table_stack[-1]["current_row"] = None
            if isinstance(current_row, list) and any(text for _, text in current_row):
                current_table_rows = self._table_stack[-1]["rows"]
                if not isinstance(current_table_rows, list):
                    raise JPXProviderError("internal JPX parser row buffer is invalid")
                current_table_rows.append(
                    _ParsedRow(
                        cells=tuple(text for _, text in current_row),
                        cell_tags=tuple(tag_ for tag_, _ in current_row),
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
        cache_dir: Path,
        regulation_urls: Mapping[str, str] | None = None,
        session: requests.Session | None = None,
        special_caution_index_url: str | None = None,
        *,
        sqlite_path: Path | None = None,
        cache_only: bool = False,
        allow_stale_snapshot: bool = False,
    ) -> None:
        self._cache_dir = Path(cache_dir) / "jpx"
        self._session = session or requests.Session()
        self._regulation_urls = dict(regulation_urls or {})
        self._special_caution_index_url = special_caution_index_url
        self._sqlite_path = Path(sqlite_path) if sqlite_path is not None else None
        self._cache_only = cache_only
        self._allow_stale_snapshot = allow_stale_snapshot
        if special_caution_index_url:
            self._regulation_urls.setdefault(
                JPX_SPECIAL_CAUTION_SOURCE_NAME, special_caution_index_url
            )

    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        if self._sqlite_path is not None:
            from ..sqlite_reader import read_jpx_regulations

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
            download_url = url
            if source_name == JPX_SPECIAL_CAUTION_SOURCE_NAME and self._special_caution_index_url:
                download_url = self._resolve_special_attention_xls_url(asof_date)
            rows = self._download_rows(source_name, download_url)
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
            from ..sqlite_cache import store_jpx_regulations

            store_jpx_regulations(
                self._sqlite_path,
                asof_date,
                flags_by_ticker=snapshot.flags_by_ticker,
                source_names=snapshot.source_names,
            )
        return snapshot

    def get_earnings_calendar_snapshot(self, asof_date: date) -> JPXEarningsCalendarSnapshot:
        if self._sqlite_path is not None:
            from ..sqlite_reader import read_jpx_earnings_calendar_snapshot

            cached = read_jpx_earnings_calendar_snapshot(
                self._sqlite_path,
                asof_date,
                allow_stale=self._allow_stale_snapshot,
            )
            if cached is not None:
                return cached
        self._raise_if_cache_only("jpx_earnings_calendar", asof_date.isoformat())
        source_urls = self._resolve_earnings_calendar_urls()
        entries: dict[str, date] = {}
        raw_count = 0
        excluded_count = 0
        for url in source_urls:
            parsed, raw, excluded = self._download_earnings_calendar(url)
            raw_count += raw
            excluded_count += excluded
            for entry in parsed:
                previous = entries.get(entry.ticker)
                if previous is not None and previous != entry.announcement_date:
                    raise JPXProviderError(
                        "conflicting JPX earnings dates for "
                        f"{entry.ticker}: {previous} and {entry.announcement_date}"
                    )
                entries[entry.ticker] = entry.announcement_date
        normalized_entries = tuple(
            JPXEarningsCalendarEntry(ticker=ticker, announcement_date=on_date)
            for ticker, on_date in sorted(entries.items(), key=lambda item: (item[1], item[0]))
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
            rejected_record_count=0,
        )
        if self._sqlite_path is not None:
            from ..sqlite_cache import store_jpx_earnings_calendar_snapshot

            store_jpx_earnings_calendar_snapshot(self._sqlite_path, snapshot)
        return snapshot

    def has_regulation_cache(self, asof_date: date) -> bool:
        if self._sqlite_path is not None:
            from ..sqlite_reader import has_jpx_regulation_data

            if has_jpx_regulation_data(self._sqlite_path, asof_date):
                return True
        if self._cache_only:
            return False
        return False

    def _regulation_cache_path(self, asof_date: date) -> Path:
        return self._cache_dir / "regulations" / f"{asof_date.isoformat()}.json"

    def _raise_if_cache_only(self, source: str, requirement: str) -> None:
        if not self._cache_only:
            return
        sqlite_label = self._sqlite_path.as_posix() if self._sqlite_path is not None else "<none>"
        raise JPXProviderError(
            f"SQLite cache incomplete for {source} ({requirement}); "
            f"sqlite={sqlite_label}. `screening run` is cache-only: bootstrap or repair "
            "SQLite before running screening."
        )

    def _warn_if_stale_cache(self, asof_date: date, payload: Mapping[str, object]) -> None:
        fetched_at_raw = payload.get("fetched_at_utc")
        if not fetched_at_raw:
            return
        try:
            fetched_at = datetime.fromisoformat(str(fetched_at_raw).replace("Z", "+00:00"))
        except ValueError:
            _LOGGER.warning("JPX regulation cache has invalid fetched_at_utc: %s", fetched_at_raw)
            return
        distance = weekday_distance(asof_date, fetched_at.astimezone(JST).date())
        if distance > _JPX_STALE_SNAPSHOT_BUSINESS_DAYS:
            _LOGGER.warning(
                "JPX regulation cache may be stale: asof=%s fetched_at_utc=%s weekdays=%s",
                asof_date.isoformat(),
                fetched_at.isoformat(),
                distance,
            )

    def bootstrap_cache(self, asof_date: date) -> dict[str, int]:
        earnings = self.get_earnings_calendar_snapshot(asof_date)
        snapshot = self.get_regulation_snapshot(asof_date)
        return {
            "earnings_calendar_rows": earnings.valid_record_count,
            "regulated_tickers": len(snapshot.flags_by_ticker),
        }

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

    def _download_earnings_calendar(
        self, url: str
    ) -> tuple[tuple[JPXEarningsCalendarEntry, ...], int, int]:
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

    def _parse_earnings_calendar_excel(
        self, content: bytes, url: str
    ) -> tuple[tuple[JPXEarningsCalendarEntry, ...], int, int]:
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
        for row_index, (_, raw_row) in enumerate(frame.iterrows()):
            cells = [re.sub(r"\s+", "", str(value)) for value in raw_row.tolist()]
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
        entries: list[JPXEarningsCalendarEntry] = []
        raw_count = 0
        excluded_count = 0
        seen: dict[str, date] = {}
        for _, raw_row in frame.iloc[header_index + 1 :].iterrows():
            values = [str(value).strip() for value in raw_row.tolist()]
            code_raw = values[code_column] if code_column < len(values) else ""
            date_raw = values[date_column] if date_column < len(values) else ""
            if not code_raw:
                continue
            if not date_raw and re.match(r"^(?:※|注|備考|note\b)", code_raw, re.IGNORECASE):
                continue
            raw_count += 1
            ticker = parse_jpx_code(code_raw)
            if not date_raw or date_raw.startswith("未定"):
                excluded_count += 1
                continue
            announcement_date = _parse_jpx_earnings_date(date_raw, url=url, ticker=ticker)
            previous = seen.get(ticker)
            if previous is not None and previous != announcement_date:
                raise JPXProviderError(
                    f"conflicting JPX earnings dates for {ticker}: "
                    f"{previous} and {announcement_date}"
                )
            if previous is None:
                seen[ticker] = announcement_date
                entries.append(
                    JPXEarningsCalendarEntry(ticker=ticker, announcement_date=announcement_date)
                )
        return tuple(entries), raw_count, excluded_count

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
            return self._parse_excel_rows(source_name, response.content, url)
        if suffix == ".html" or content_type == "text/html":
            return self._parse_html_rows(source_name, response.content, url, content_type_header)
        raise JPXProviderError(f"unsupported JPX regulation source format: {url}")

    def _validate_allowed_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme != _JPX_ALLOWED_SCHEME or parsed.netloc != _JPX_ALLOWED_HOST:
            raise JPXProviderError(f"JPX regulation source must use https://www.jpx.co.jp/: {url}")

    def _resolve_special_attention_xls_url(self, asof_date: date) -> str:
        # The index publishes only the latest xls; backfill control is enforced
        # one layer up via the --allow-stale-jpx guard (issue #19).
        del asof_date
        if not self._special_caution_index_url:
            raise JPXProviderError("JPX_SPECIAL_CAUTION_INDEX_URL is not configured")

        index_url = self._special_caution_index_url
        self._validate_allowed_url(index_url)
        response = self._session.get(index_url, timeout=_HTTP_TIMEOUT_SECONDS)
        if response.status_code >= 400:
            raise JPXProviderError(
                f"failed to download JPX special caution index: {index_url} "
                f"(status={response.status_code})"
            )

        content_type_header = response.headers.get("content-type", "")
        html = self._decode_html_text(response.content, index_url, content_type_header)
        indexer = _JpxHtmlIndexer()
        indexer.feed(html)

        candidates: list[tuple[int, int, str]] = []
        for position, link in enumerate(indexer.links):
            resolved_url = urljoin(index_url, link.href)
            parsed = urlparse(resolved_url)
            suffix = Path(parsed.path).suffix.lower()
            if suffix not in _JPX_EXCEL_SUFFIXES:
                continue
            haystack = " ".join(
                part
                for part in (
                    parsed.path.lower(),
                    link.text,
                    link.preceding_h2,
                    link.preceding_h3,
                )
                if part
            )
            if (
                _JPX_SPECIAL_CAUTION_MARGIN_LINK_HINT not in parsed.path.lower()
                and JPX_SPECIAL_CAUTION_SOURCE_NAME not in haystack
                and "個別銘柄信用取引残高表" not in haystack
            ):
                continue
            date_match = re.search(r"20\d{6}", parsed.path)
            # Prefer the newest date token; if JPX ever omits dates, fall back
            # to document order by keeping zero-date candidates sortable.
            date_key = int(date_match.group(0)) if date_match else 0
            candidates.append((date_key, position, resolved_url))

        if not candidates:
            raise JPXProviderError(f"failed to locate JPX special caution Excel link: {index_url}")

        resolved_url = max(candidates)[2]
        self._validate_allowed_url(resolved_url)
        return resolved_url

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

    def _parse_excel_rows(self, source_name: str, content: bytes, url: str) -> list[dict[str, str]]:
        try:
            import pandas as pd
        except ModuleNotFoundError as exc:
            raise JPXProviderError("pandas is required to read JPX Excel sources") from exc

        try:
            if source_name == "特別注意銘柄":
                return self._parse_special_alert_margin_rows(pd, content, source_name, url)
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
            if _TRADING_HALT_EMPTY_MARKER in html:
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

    def _parse_special_alert_margin_rows(
        self, pd: Any, content: bytes, source_name: str, url: str
    ) -> list[dict[str, str]]:
        # Official JPX margin xls marks current "特別注意銘柄" rows with "○" in the
        # second column and stores the 5-char security code in the seventh column.
        # Keep this logic source-specific so the file layout remains understandable
        # from code and tests without relying on ad-hoc HTML scraping.
        del url
        frame = pd.read_excel(BytesIO(content), header=None, dtype=str).fillna("")
        rows: list[dict[str, str]] = []
        for _, raw_row in frame.iloc[7:].iterrows():
            values = [str(value).strip() for value in raw_row.tolist()]
            if len(values) <= 6 or values[1] != "○":
                continue
            rows.append({"code": values[6], "flag": source_name})
        return rows


def parse_jpx_code(code: object) -> str:
    raw = str(code or "").strip().upper()
    if len(raw) == 4:
        return normalize_ticker(raw)
    if len(raw) == 5 and raw[:4].isalnum() and raw.endswith("0"):
        return normalize_ticker(raw[:4])
    raise JPXProviderError(f"invalid JPX code: {code!r}")


def _parse_jpx_earnings_date(raw: str, *, url: str, ticker: str) -> date:
    normalized = raw.strip().replace("年", "-").replace("月", "-").replace("日", "")
    normalized = normalized.replace("/", "-").replace(".", "-")[:10]
    try:
        return date.fromisoformat(normalized)
    except ValueError as exc:
        raise JPXProviderError(f"invalid JPX earnings date for {ticker}: {raw!r} ({url})") from exc
