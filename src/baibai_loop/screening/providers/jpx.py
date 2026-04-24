from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlparse
import re

import requests

from ..schema import normalize_ticker

_JPX_ALLOWED_SCHEME = "https"
_JPX_ALLOWED_HOST = "www.jpx.co.jp"


class JPXProviderError(RuntimeError):
    """Raised when required JPX public CSV/Excel sources are unavailable."""


@dataclass(frozen=True)
class JPXRegulationSnapshot:
    flags_by_ticker: Mapping[str, tuple[str, ...]]
    source_names: Sequence[str]


class _HTMLTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._cell_fragments: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "tr":
            self._current_row = []
            return
        if self._current_row is None:
            return
        if tag in {"td", "th"}:
            self._cell_fragments = []
            return
        if tag == "br" and self._cell_fragments is not None:
            self._cell_fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell_fragments is not None and self._current_row is not None:
            value = "".join(self._cell_fragments).replace("\xa0", " ")
            normalized = re.sub(r"\s+", " ", value).strip()
            self._current_row.append(normalized)
            self._cell_fragments = None
            return
        if tag == "tr" and self._current_row is not None:
            if any(cell for cell in self._current_row):
                self.rows.append(self._current_row)
            self._current_row = None

    def handle_data(self, data: str) -> None:
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
        cache_path = self._cache_dir / "regulations" / f"{asof_date.isoformat()}.json"
        if cache_path.exists():
            import json

            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            return JPXRegulationSnapshot(
                flags_by_ticker={ticker: tuple(flags) for ticker, flags in payload.get("flags_by_ticker", {}).items()},
                source_names=tuple(payload.get("source_names", ())),
            )

        if not self._regulation_urls:
            raise JPXProviderError("JPX regulation data is required but no public CSV/Excel URL is configured")

        flags: dict[str, set[str]] = {}
        for source_name, url in self._regulation_urls.items():
            rows = self._download_rows(source_name, url)
            for row in rows:
                ticker_raw = row.get("ticker") or row.get("code") or row.get("銘柄コード") or row.get("コード")
                flag = row.get("flag") or row.get("規制区分") or row.get("status") or source_name
                if not ticker_raw:
                    continue
                ticker = parse_jpx_code(ticker_raw)
                flags.setdefault(ticker, set()).add(str(flag))

        snapshot = JPXRegulationSnapshot(
            flags_by_ticker={ticker: tuple(sorted(values)) for ticker, values in sorted(flags.items())},
            source_names=tuple(sorted(self._regulation_urls.keys())),
        )
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        import json

        cache_path.write_text(
            json.dumps(
                {
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

    def bootstrap_cache(self, asof_date: date) -> dict[str, int]:
        snapshot = self.get_regulation_snapshot(asof_date)
        return {"regulated_tickers": len(snapshot.flags_by_ticker)}

    def _download_rows(self, source_name: str, url: str) -> list[dict[str, str]]:
        self._validate_allowed_url(url)
        response = self._session.get(url, timeout=30)
        if response.status_code >= 400:
            raise JPXProviderError(f"failed to download JPX regulation source: {url}")
        suffix = Path(urlparse(url).path).suffix.lower()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if suffix == ".csv":
            return self._parse_csv_rows(response.content, url)
        if suffix in {".xls", ".xlsx"}:
            return self._parse_excel_rows(source_name, response.content, url)
        if suffix == ".html" or content_type == "text/html":
            return self._parse_html_rows(source_name, response.content, url)
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

    def _parse_html_rows(self, source_name: str, content: bytes, url: str) -> list[dict[str, str]]:
        html = self._decode_html_text(content, url)
        if source_name == "整理銘柄":
            return self._parse_reorganization_html_rows(html, url)
        if source_name == "取引停止":
            return self._parse_trading_halt_html_rows(html, url)
        if source_name == "上場廃止警告":
            return self._parse_delisting_warning_html_rows(html, url)
        raise JPXProviderError(f"unsupported JPX HTML parser source: {source_name}")

    def _decode_html_text(self, content: bytes, url: str) -> str:
        for encoding in ("utf-8-sig", "utf-8", "cp932"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise JPXProviderError(f"failed to decode JPX HTML source: {url}")

    def _parse_reorganization_html_rows(self, html: str, url: str) -> list[dict[str, str]]:
        section = self._extract_html_section(html, heading_text="整理銘柄", url=url)
        table_html = self._extract_html_table(section, url=url, selector="h3.subhead-title span=整理銘柄 + table.fixedhead")
        return self._rows_from_html_table(
            table_html,
            source_name="整理銘柄",
            url=url,
            header_rows=1,
            code_column=2,
            expected_header="コード",
            selector="h3.subhead-title span=整理銘柄 + table.fixedhead",
        )

    def _parse_trading_halt_html_rows(self, html: str, url: str) -> list[dict[str, str]]:
        section = self._extract_html_section(html, heading_text="本日の売買停止情報", url=url)
        if "現在、該当する情報はありません。" in section:
            return []
        table_html = self._extract_html_table(section, url=url, selector="h2.heading-title-mu span=本日の売買停止情報 + table.fixedhead.rows-2")
        return self._rows_from_html_table(
            table_html,
            source_name="取引停止",
            url=url,
            header_rows=2,
            code_column=1,
            expected_header="コード",
            selector="h2.heading-title-mu span=本日の売買停止情報 + table.fixedhead.rows-2",
        )

    def _parse_delisting_warning_html_rows(self, html: str, url: str) -> list[dict[str, str]]:
        table_html = self._extract_html_table_with_header(
            html,
            header_text="上場廃止日",
            url=url,
            selector="table.fixedhead th=上場廃止日",
        )
        return self._rows_from_html_table(
            table_html,
            source_name="上場廃止警告",
            url=url,
            header_rows=1,
            code_column=2,
            expected_header="コード",
            selector="table.fixedhead th=上場廃止日",
        )

    def _extract_html_section(self, html: str, heading_text: str, url: str) -> str:
        pattern = re.compile(
            rf"<h[23][^>]*>\s*<span>\s*{re.escape(heading_text)}\s*</span>\s*</h[23]>\s*(.*?)(?=<h[23][^>]*>|</section>)",
            re.DOTALL,
        )
        match = pattern.search(html)
        if match is None:
            raise JPXProviderError(f"failed to locate JPX HTML section {heading_text!r}: {url}")
        return match.group(1)

    def _extract_html_table(self, html: str, url: str, selector: str) -> str:
        match = re.search(r"(<table[^>]*class=\"[^\"]*fixedhead[^\"]*\"[^>]*>.*?</table>)", html, re.DOTALL)
        if match is None:
            raise JPXProviderError(f"failed to locate JPX HTML table for {selector}: {url}")
        return match.group(1)

    def _extract_html_table_with_header(self, html: str, header_text: str, url: str, selector: str) -> str:
        pattern = re.compile(
            rf"(<table[^>]*class=\"[^\"]*fixedhead[^\"]*\"[^>]*>.*?<th[^>]*>\s*{re.escape(header_text)}\s*</th>.*?</table>)",
            re.DOTALL,
        )
        match = pattern.search(html)
        if match is None:
            raise JPXProviderError(f"failed to locate JPX HTML table for {selector}: {url}")
        return match.group(1)

    def _rows_from_html_table(
        self,
        table_html: str,
        source_name: str,
        url: str,
        header_rows: int,
        code_column: int,
        expected_header: str,
        selector: str,
    ) -> list[dict[str, str]]:
        parser = _HTMLTableParser()
        parser.feed(table_html)
        rows = parser.rows
        if len(rows) < header_rows:
            raise JPXProviderError(f"failed to parse JPX HTML table rows for {selector}: {url}")
        header = rows[0]
        if len(header) <= code_column or header[code_column] != expected_header:
            raise JPXProviderError(f"unexpected JPX HTML table layout for {selector}: {url}")

        parsed_rows: list[dict[str, str]] = []
        for row in rows[header_rows:]:
            if len(row) <= code_column:
                raise JPXProviderError(f"unexpected JPX HTML row layout for {selector}: {url}")
            code = row[code_column]
            if not code:
                raise JPXProviderError(f"missing JPX code in HTML row for {selector}: {url}")
            parsed_rows.append({"code": code, "flag": source_name})
        return parsed_rows

    def _parse_special_alert_margin_rows(self, pd: object, content: bytes, source_name: str, url: str) -> list[dict[str, str]]:
        # Official JPX margin xls marks current "特別注意銘柄" rows with "○" in the
        # second column and stores the 5-char security code in the seventh column.
        # Keep this logic source-specific so the file layout remains understandable
        # from code and tests without relying on ad-hoc HTML scraping.
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
