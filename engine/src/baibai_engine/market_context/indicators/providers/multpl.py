from __future__ import annotations

from datetime import date
from html.parser import HTMLParser

from baibai_engine.collection.economic_sources import SourceDefinition

from ..observations import NativeRecords
from ..read_contracts import MULTPL_SPEC
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    fetch_text,
)

# multpl.com rate-limits the default requests User-Agent; a browser UA is required.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_HISTORY_DATE_FORMAT = "%b %d, %Y"
_HISTORY_FLOOR_BY_SLUG = {
    "shiller-pe": date(1871, 2, 1),
    "s-p-500-pe-ratio": date(1871, 1, 1),
    "s-p-500-earnings-yield": date(1871, 1, 1),
}


class MultplSource:
    """Retain dated table cells and the page's own source explanation for L2."""

    spec = MULTPL_SPEC
    name = spec.name

    def fetch_records(
        self,
        series: SourceDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> NativeRecords:
        text = fetch_text(
            session,
            series.source_url,
            params=None,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            headers={"User-Agent": _USER_AGENT},
            context=context,
        )
        return parse_multpl_table(series, text, start=start, end=end)


def parse_multpl_table(
    series: SourceDefinition,
    text: str,
    *,
    start: date,
    end: date,
) -> NativeRecords:
    parser = _MultplTableParser()
    parser.feed(text)
    parser.close()
    if not parser.found_table or not parser.rows:
        raise IndicatorsProviderError(
            f"multpl: table missing or empty for {series.provider_series_id}"
        )
    floor = _HISTORY_FLOOR_BY_SLUG.get(series.provider_series_id)
    if floor is None:
        raise IndicatorsProviderError(
            f"multpl: unknown history floor for {series.provider_series_id}"
        )

    dated_rows: list[tuple[date, dict[str, object]]] = []
    for index, (raw_date, raw_value, cell_annotations) in enumerate(parser.rows):
        try:
            observed_at = date.strptime(raw_date.strip(), _HISTORY_DATE_FORMAT)
        except ValueError as exc:
            raise IndicatorsProviderError(
                f"multpl: invalid table date for {series.provider_series_id}: {raw_date!r}"
            ) from exc
        row: dict[str, object] = {
            "date_text": raw_date,
            "value_text": raw_value,
            "table_row": index,
        }
        if cell_annotations:
            row["value_annotations"] = cell_annotations
        if parser.caption_text is not None:
            row["caption_text"] = parser.caption_text
        if parser.description_text is not None:
            row["description_text"] = parser.description_text
            row["description_links"] = parser.description_links
        dated_rows.append((observed_at, row))

    # The first row is the current level, possibly dated on a month start. It is
    # not proof that a monthly-history row for that month has been published.
    if start <= floor:
        history_dates = {day for day, _ in dated_rows[1:] if day.day == 1}
        if not history_dates or min(history_dates) != floor:
            raise IndicatorsProviderError(
                f"multpl: history for {series.provider_series_id} must start at {floor}"
            )
        published = [day for day in history_dates if day <= end]
        expected = floor
        while expected <= max(published, default=floor):
            if expected not in history_dates:
                raise IndicatorsProviderError(
                    f"multpl: missing monthly row for {series.provider_series_id}: {expected}"
                )
            expected = _next_month(expected)

    records: dict[date, list[dict[str, object]]] = {}
    for observed_at, row in dated_rows:
        if start <= observed_at <= end:
            records.setdefault(observed_at, []).append(row)
    return records


class _MultplTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found_table = False
        self.rows: list[tuple[str, str, list[dict[str, str]]]] = []
        self.caption_text: str | None = None
        self.description_text: str | None = None
        self.description_links: list[dict[str, str]] = []
        self._table = False
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._row_annotations: list[dict[str, str]] = []
        self._cell_annotations: list[dict[str, str]] | None = None
        self._abbr_parts: list[str] | None = None
        self._abbr_title: str | None = None
        self._caption_parts: list[str] | None = None
        self._description_depth = 0
        self._description_parts: list[str] = []
        self._link_href: str | None = None
        self._link_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id") == "description" and self._description_depth == 0:
            self._description_depth = 1
            self._description_parts = []
        elif self._description_depth:
            if tag not in {"area", "br", "hr", "img", "input", "meta", "link"}:
                self._description_depth += 1
            if tag == "a" and attributes.get("href"):
                self._link_href = attributes["href"]
                self._link_parts = []
        if tag == "table" and attributes.get("id") == "datatable":
            self.found_table = True
            self._table = True
        elif self._table and tag == "caption":
            self._caption_parts = []
        elif self._table and tag == "tr":
            self._row = []
            self._row_annotations = []
        elif self._table and self._row is not None and tag == "td":
            self._cell = []
            self._cell_annotations = [] if len(self._row) == 1 else None
        elif self._cell_annotations is not None and tag == "abbr":
            self._abbr_parts = []
            self._abbr_title = attributes.get("title")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)
        if self._abbr_parts is not None:
            self._abbr_parts.append(data)
        if self._caption_parts is not None:
            self._caption_parts.append(data)
        if self._description_depth:
            self._description_parts.append(data)
            if self._link_href is not None:
                self._link_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "abbr" and self._abbr_parts is not None:
            annotation = {"text": "".join(self._abbr_parts)}
            if self._abbr_title is not None:
                annotation["title"] = self._abbr_title
            if self._cell_annotations is not None:
                self._cell_annotations.append(annotation)
            self._abbr_parts = None
            self._abbr_title = None
        elif self._table and tag == "td" and self._cell is not None:
            if self._row is not None:
                if len(self._row) == 1:
                    self._row_annotations = self._cell_annotations or []
                self._row.append("".join(self._cell))
            self._cell = None
            self._cell_annotations = None
        elif self._table and tag == "tr" and self._row is not None:
            if self._row:
                if len(self._row) != 2:
                    raise IndicatorsProviderError("multpl: table row must have exactly two cells")
                self.rows.append((self._row[0], self._row[1], self._row_annotations))
            self._row = None
        elif self._table and tag == "caption" and self._caption_parts is not None:
            self.caption_text = "".join(self._caption_parts)
            self._caption_parts = None
        elif self._table and tag == "table":
            self._table = False
        if self._description_depth and tag not in {
            "area",
            "br",
            "hr",
            "img",
            "input",
            "meta",
            "link",
        }:
            if tag == "a" and self._link_href is not None:
                self.description_links.append(
                    {
                        "text": "".join(self._link_parts),
                        "href": self._link_href,
                    }
                )
                self._link_href = None
            self._description_depth -= 1
            if self._description_depth == 0:
                self.description_text = "".join(self._description_parts)


def _next_month(value: date) -> date:
    return date(value.year + 1, 1, 1) if value.month == 12 else date(value.year, value.month + 1, 1)
