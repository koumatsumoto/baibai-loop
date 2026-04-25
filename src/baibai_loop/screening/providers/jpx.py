from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlparse

import requests

from ..date_utils import weekday_distance
from ..render import JST
from ..schema import normalize_ticker

_JPX_ALLOWED_SCHEME = "https"
_JPX_ALLOWED_HOST = "www.jpx.co.jp"
_JPX_CACHE_SCHEMA_VERSION = 1
_JPX_STALE_SNAPSHOT_BUSINESS_DAYS = 7
_TRADING_HALT_EMPTY_MARKER = "現在、該当する情報はありません。"
_HTTP_TIMEOUT_SECONDS = 30
_LOGGER = logging.getLogger(__name__)


class JPXProviderError(RuntimeError):
    """Raised when required JPX public CSV/Excel/HTML sources are unavailable."""


@dataclass(frozen=True)
class JPXRegulationSnapshot:
    flags_by_ticker: Mapping[str, tuple[str, ...]]
    source_names: Sequence[str]


@dataclass(frozen=True)
class _ParsedRow:
    cells: tuple[str, ...]
    cell_tags: tuple[str, ...]


@dataclass(frozen=True)
class _ParsedTable:
    classes: tuple[str, ...]
    rows: tuple[_ParsedRow, ...]
    preceding_h2: str | None
    preceding_h3: str | None
    header_th_texts: tuple[str, ...]


class _JpxHtmlIndexer(HTMLParser):
    """Parse JPX HTML once and surface only outer-most `<table>` blocks.

    Nested tables are dropped by construction (only tables closing at stack
    depth 0 are recorded), so callers can match by class / preceding heading /
    header text without writing brittle regex.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[_ParsedTable] = []
        self._table_stack: list[dict[str, object]] = []
        self._cell_fragments: list[str] | None = None
        self._cell_tag: str | None = None
        self._heading_buffer: list[str] | None = None
        self._heading_tag: str | None = None
        self._last_h2: str | None = None
        self._last_h3: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
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
                rows = self._table_stack[-1]["rows"]
                assert isinstance(rows, list)
                rows.append(
                    _ParsedRow(
                        cells=tuple(text for _, text in current_row),
                        cell_tags=tuple(tag_ for tag_, _ in current_row),
                    )
                )
            return
        if tag == "table":
            data = self._table_stack.pop()
            rows_payload = data["rows"]
            assert isinstance(rows_payload, list)
            rows: tuple[_ParsedRow, ...] = tuple(rows_payload)
            classes_payload = data["classes"]
            assert isinstance(classes_payload, tuple)
            header_th_texts: tuple[str, ...] = ()
            for row in rows:
                if row.cell_tags and all(t == "th" for t in row.cell_tags):
                    header_th_texts = row.cells
                    break
            preceding_h2 = data["preceding_h2"]
            preceding_h3 = data["preceding_h3"]
            assert preceding_h2 is None or isinstance(preceding_h2, str)
            assert preceding_h3 is None or isinstance(preceding_h3, str)
            parsed = _ParsedTable(
                classes=classes_payload,
                rows=rows,
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


class JPXProvider:
    """JPX public regulation provider with bounded HTML parsing for required sources."""

    def __init__(
        self,
        cache_dir: Path,
        regulation_urls: Mapping[str, str] | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._cache_dir = Path(cache_dir) / "jpx"
        self._session = session or requests.Session()
        self._regulation_urls = dict(regulation_urls or {})

    def get_regulation_snapshot(self, asof_date: date) -> JPXRegulationSnapshot:
        cache_path = self._regulation_cache_path(asof_date)
        if cache_path.exists():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            schema_version = payload.get("schema_version")
            if schema_version not in (None, _JPX_CACHE_SCHEMA_VERSION):
                raise JPXProviderError(
                    f"incompatible JPX cache schema version: {schema_version}"
                )
            self._warn_if_stale_cache(asof_date, payload)
            return JPXRegulationSnapshot(
                flags_by_ticker={
                    ticker: tuple(flags)
                    for ticker, flags in payload.get("flags_by_ticker", {}).items()
                },
                source_names=tuple(payload.get("source_names", ())),
            )

        if not self._regulation_urls:
            raise JPXProviderError(
                "JPX regulation data is required but no public CSV/Excel/HTML URL is configured"
            )

        flags: dict[str, set[str]] = {}
        for source_name, url in self._regulation_urls.items():
            rows = self._download_rows(source_name, url)
            for row in rows:
                ticker_raw = (
                    row.get("ticker")
                    or row.get("code")
                    or row.get("銘柄コード")
                    or row.get("コード")
                )
                flag = row.get("flag") or row.get("規制区分") or row.get("status") or source_name
                if not ticker_raw:
                    continue
                ticker = parse_jpx_code(ticker_raw)
                flags.setdefault(ticker, set()).add(str(flag))

        snapshot = JPXRegulationSnapshot(
            flags_by_ticker={
                ticker: tuple(sorted(values)) for ticker, values in sorted(flags.items())
            },
            source_names=tuple(sorted(self._regulation_urls.keys())),
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {
                    "schema_version": _JPX_CACHE_SCHEMA_VERSION,
                    "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
                    "flags_by_ticker": snapshot.flags_by_ticker,
                    "source_names": list(snapshot.source_names),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return snapshot

    def has_regulation_cache(self, asof_date: date) -> bool:
        return self._regulation_cache_path(asof_date).exists()

    def _regulation_cache_path(self, asof_date: date) -> Path:
        return self._cache_dir / "regulations" / f"{asof_date.isoformat()}.json"

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
        snapshot = self.get_regulation_snapshot(asof_date)
        return {"regulated_tickers": len(snapshot.flags_by_ticker)}

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
            raise JPXProviderError(
                f"JPX regulation source must use https://www.jpx.co.jp/: {url}"
            )

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

    def _parse_excel_rows(
        self, source_name: str, content: bytes, url: str
    ) -> list[dict[str, str]]:
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

    def _decode_html_text(
        self, content: bytes, url: str, content_type_header: str = ""
    ) -> str:
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

    def _parse_delisting_warning_html_rows(
        self, html: str, url: str
    ) -> list[dict[str, str]]:
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
        raise JPXProviderError(
            f"failed to locate JPX HTML table for {error_label}: {url}"
        )

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
            raise JPXProviderError(
                f"failed to parse JPX HTML table rows for {error_label}: {url}"
            )
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
            raise JPXProviderError(
                f"unexpected JPX HTML table layout for {error_label}: {url}"
            )

        parsed_rows: list[dict[str, str]] = []
        for row in rows[header_rows:]:
            if len(row.cells) <= code_column:
                raise JPXProviderError(
                    f"unexpected JPX HTML row layout for {error_label}: {url}"
                )
            if row.cell_tags and all(tag == "th" for tag in row.cell_tags):
                raise JPXProviderError(
                    f"unexpected JPX HTML data row layout for {error_label}: {url}"
                )
            code = row.cells[code_column]
            if not code:
                raise JPXProviderError(
                    f"missing JPX code in HTML row for {error_label}: {url}"
                )
            parsed_rows.append({"code": code, "flag": source_name})
        return parsed_rows

    def _parse_special_alert_margin_rows(
        self, pd: object, content: bytes, source_name: str, url: str
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
