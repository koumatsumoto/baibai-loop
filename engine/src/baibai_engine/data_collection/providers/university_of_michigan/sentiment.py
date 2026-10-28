from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date

from baibai_engine.data_collection.provider_contracts import ProviderSpec
from baibai_engine.data_collection.receipts.native_sources.records import NativeRecords
from baibai_engine.data_collection.catalog import SourceDefinition

from baibai_engine.data_collection.transport.context import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    fetch_text,
)

# The Surveys of Consumers publish their index tables as CSV keyed by a spelled-out
# month name and a separate year column, so the observation date is assembled from
# the pair. The names are English in the source, so they are fixed here instead of
# being read from the runtime locale.
_MONTH_NUMBERS = {
    "January": 1,
    "February": 2,
    "March": 3,
    "April": 4,
    "May": 5,
    "June": 6,
    "July": 7,
    "August": 8,
    "September": 9,
    "October": 10,
    "November": 11,
    "December": 12,
}


class UmichScaSource:
    """Acquire the complete University of Michigan Surveys of Consumers table.

    The source URL identifies the table. Acquisition retains its columns and
    missing cells; selecting and interpreting an index value belong to L2.
    """

    spec = ProviderSpec(name="umich_sca", all_history_start=date(1952, 11, 1))
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
            context=context,
        )
        return parse_umich_table(series, text, start=start, end=end)


def parse_umich_table(
    series: SourceDefinition, text: str, *, start: date, end: date
) -> NativeRecords:
    """Retain the whole Surveys of Consumers row, including missing cells.

    A row whose month or year cannot be read is rejected rather than skipped: the
    table has one shape, so an unreadable row means the source changed and the
    fetch must fail instead of quietly returning a shorter history.
    """

    reader = csv.DictReader(io.StringIO(text))
    columns = reader.fieldnames or []
    for column in ("Month", "YYYY"):
        if column not in columns:
            raise IndicatorsProviderError(f"UMich table missing column {column}")
    if len(columns) != len(set(columns)):
        raise IndicatorsProviderError("UMich table has duplicate columns")
    records: dict[date, list[dict[str, object]]] = defaultdict(list)
    for row in reader:
        if None in row:
            raise IndicatorsProviderError("UMich table row has more fields than its header")
        observed_at = _observed_at(row)
        if start <= observed_at <= end:
            records[observed_at].append(dict(row))
    return dict(records)


def _observed_at(row: dict[str, str | None]) -> date:
    month = (row.get("Month") or "").strip()
    year = (row.get("YYYY") or "").strip()
    if month not in _MONTH_NUMBERS or not year.isdigit():
        raise IndicatorsProviderError(
            f"UMich table row is not a month of a year: {month!r} {year!r}"
        )
    return date(int(year), _MONTH_NUMBERS[month], 1)
