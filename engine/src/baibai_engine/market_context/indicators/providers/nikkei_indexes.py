from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator, Mapping
from datetime import date
from html.parser import HTMLParser

from baibai_engine.collection.economic_sources import SourceDefinition

from ..observations import NativeRecords
from .base import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderSpec,
    fetch_text,
)

# The Nikkei index valuation page renders its PER/PBR history with JavaScript,
# but the table itself is served by the statistics/dataload endpoint one calendar
# month at a time (list=per|pbr, year=YYYY, month=M). We read that endpoint
# directly (no browser); its WAF requires a browser User-Agent plus the AJAX
# headers the page's script sends, and rejects the default client otherwise.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_DATALOAD_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://indexes.nikkei.co.jp/nkave/archives/data",
    "X-Requested-With": "XMLHttpRequest",
}

_METRICS = frozenset({"per", "pbr"})


class NikkeiIndexesSource:
    """Retain both published PER/PBR columns; L2 selects the weighted average."""

    spec = ProviderSpec(name="nikkei_indexes", all_history_start=date(2010, 1, 1))
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
        metric = series.provider_series_id
        if metric not in _METRICS:
            supported = ", ".join(sorted(_METRICS))
            raise IndicatorsProviderError(
                f"unsupported nikkei_indexes metric {metric!r}; supported: {supported}"
            )
        by_date: dict[date, list[Mapping[str, object]]] = defaultdict(list)
        for year, month in _months(start, end):
            text = fetch_text(
                session,
                series.source_url,
                params={"list": metric, "type": "1", "year": str(year), "month": str(month)},
                max_bytes=MAX_CSV_RESPONSE_BYTES,
                headers=_DATALOAD_HEADERS,
                context=context,
            )
            for day, records in parse_nikkei_valuation(series, text, start=start, end=end).items():
                by_date[day].extend(records)
        return dict(by_date)


def parse_nikkei_valuation(
    series: SourceDefinition,
    text: str,
    *,
    start: date,
    end: date,
) -> NativeRecords:
    parser = _ValuationRows()
    parser.feed(text)
    parser.close()
    if not parser.rows:
        raise IndicatorsProviderError("Nikkei valuation data rows missing")
    records: dict[date, list[dict[str, object]]] = defaultdict(list)
    for cells in parser.rows:
        if not cells or not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", cells[0].strip()):
            raise IndicatorsProviderError("Nikkei valuation row has an invalid date")
        if len(cells) != 3:
            raise IndicatorsProviderError("Nikkei valuation row must have three cells")
        observed_at = date.fromisoformat(cells[0].strip().replace(".", "-"))
        if start <= observed_at <= end:
            records[observed_at].append(
                {"date_text": cells[0], "weighted_text": cells[1], "index_based_text": cells[2]}
            )
    return dict(records)


class _ValuationRows(HTMLParser):
    """Read data cells, leaving numeric and missing-marker interpretation to L2."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self.cells: list[str] = []
        self.data_row = False
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.cells = []
            self.data_row = False
        elif tag == "td":
            self.cell = []

    def handle_comment(self, data: str) -> None:
        if data.strip() == "daily_changing":
            self.data_row = True

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self.cell is not None:
            self.cells.append("".join(self.cell))
            self.cell = None
        elif tag == "tr" and self.cells:
            if self.data_row or re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", self.cells[0].strip()):
                self.rows.append(self.cells)
            self.cells = []


def _months(start: date, end: date) -> Iterator[tuple[int, int]]:
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        yield year, month
        month += 1
        if month > 12:
            month = 1
            year += 1
