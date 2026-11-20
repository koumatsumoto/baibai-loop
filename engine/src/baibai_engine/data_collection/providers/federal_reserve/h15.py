from __future__ import annotations

import csv
import io
from datetime import date
from html.parser import HTMLParser

from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.provider_contracts import ProviderSpec
from baibai_engine.data_collection.receipts.native_sources.records import NativeRecords
from baibai_engine.data_collection.transport.context import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderFetchStoppedError,
    fetch_text,
)

# The Board publishes one fixed download URL for this preformatted package.
# The receipt window is local; changing URL parameters requests a different resource.
_H15_AUTOMATED_PACKAGE = {
    "filetype": "csv",
    "from": "",
    "label": "include",
    "lastobs": "",
    "layout": "seriescolumn",
    "rel": "H15",
    "series": "bf17364827e38702b42a58cf8eaa3f78",
    "to": "",
    "type": "package",
}
_HEADER_PREFIXES = ('"Time Period"', "Time Period,")


class FrbH15Source:
    """Federal Reserve H.15 source yields, without local spread calculations."""

    spec = ProviderSpec(name="frb_h15", all_history_start=date(1962, 1, 1))

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
            params=_H15_AUTOMATED_PACKAGE,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
            allow_redirects=False,
        )
        return parse_h15_csv(text, start=start, end=end)


class _ResponseTitle(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.in_title = False
        self.found_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title" and not self.found_title:
            self.in_title = True
            self.found_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.parts.append(data)


def snippet(text: str) -> str:
    """What the source sent, flattened to one bounded line for a batch log."""

    return " ".join(text.split())[:160]


def parse_h15_csv(text: str, *, start: date, end: date) -> NativeRecords:
    lines = text.splitlines(keepends=True)
    header_index = next(
        (index for index, line in enumerate(lines) if line.startswith(_HEADER_PREFIXES)),
        None,
    )
    if header_index is None:
        title = _ResponseTitle()
        title.feed(text)
        if " ".join("".join(title.parts).split()).casefold() == "access denied":
            raise ProviderFetchStoppedError(
                f"FRB H.15 successful response explicitly denies access: {snippet(text)!r}"
            )
        # Surface what the server actually returned (block page, error HTML, or a
        # changed layout) so a remote-only failure is diagnosable from batch logs.
        raise IndicatorsProviderError(
            f"FRB H.15 CSV missing Time Period header; response starts with: {snippet(text)!r}"
        )
    reader = csv.DictReader(io.StringIO("".join(lines[header_index:])))
    columns = reader.fieldnames or []
    if len(columns) != len(set(columns)):
        raise IndicatorsProviderError("FRB H.15 CSV has duplicate columns")
    # Preserve package columns before any individual yield is selected in L2.
    # Preamble lines are literal source metadata, not an inferred schema/unit.
    preamble = [line.rstrip("\r\n") for line in lines[:header_index]]
    records: dict[date, list[dict[str, object]]] = {}
    has_observations = False
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise IndicatorsProviderError("FRB H.15 CSV row does not match its columns")
        observed_at_raw = row.get("Time Period")
        if not observed_at_raw:
            raise IndicatorsProviderError("FRB H.15 CSV row has no Time Period")
        observed_at = date.fromisoformat(observed_at_raw)
        has_observations = True
        if start <= observed_at <= end:
            records.setdefault(observed_at, []).append({"row": row, "preamble": preamble})
    # This URL serves the entire history. A header alone is not evidence that
    # every day in the local receipt window was absent.
    if not has_observations:
        raise ProviderFetchStoppedError("FRB H.15 full package contains no observations")
    return records
