from __future__ import annotations

import io
import math
import zipfile
from collections.abc import Sequence
from datetime import date, datetime

import openpyxl

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    MAX_ZIP_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    fetch_bytes,
    record_observation,
)


class BojProvider:
    """Bank of Japan long time-series Excel workbook (no auth).

    BOJ publishes stable ``.xlsx`` long-series workbooks (e.g. the monetary base
    at ``other/mb/mblong.xlsx``); the stat-search interactive CSV is session /
    cgi based and not a stable GET URL. The first sheet holds the headline series
    with a month-end date in the date column and values in numbered data columns.
    ``provider_series_id`` is the 1-based value column (e.g. "3" = Monetary Base
    in mblong.xlsx). Observations are normalised to the first of the month so they
    align with the other monthly series (FRED / e-Stat).
    """

    name = "boj"

    def fetch(
        self,
        series: SeriesDefinition,
        *,
        start: date,
        end: date,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> list[ObservationRecord]:
        content = fetch_bytes(
            session,
            series.source_url,
            params=None,
            max_bytes=MAX_ZIP_RESPONSE_BYTES,
            context=context,
        )
        return parse_boj_xlsx(series, content, start=start, end=end)


def parse_boj_xlsx(
    series: SeriesDefinition, content: bytes, *, start: date, end: date
) -> list[ObservationRecord]:
    value_col = _value_column_index(series.provider_series_id)
    if not zipfile.is_zipfile(io.BytesIO(content)):
        raise IndicatorsProviderError("BOJ response is not a .xlsx (zip) workbook")
    observations: list[ObservationRecord] = []
    saw_date_row = False
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        try:
            worksheet = workbook[workbook.sheetnames[0]]
            for row in worksheet.iter_rows(values_only=True):
                cells: Sequence[object] = row
                observed_at = _row_month(cells)
                if observed_at is None:
                    continue
                saw_date_row = True
                cell = cells[value_col - 1] if value_col - 1 < len(cells) else None
                value = _cell_float(cell)
                if value is None:
                    continue
                if start <= observed_at <= end:
                    observations.append(
                        record_observation(series, observed_at=observed_at, value=value)
                    )
        finally:
            workbook.close()
    except Exception as exc:
        # openpyxl / ElementTree raise opaque third-party errors on a corrupt or
        # non-xlsx workbook (InvalidFileException, ParseError, IndexError on an
        # empty workbook); convert them so the indicators CLI never leaks a traceback.
        raise IndicatorsProviderError(f"BOJ workbook could not be read: {exc}") from exc
    if not saw_date_row:
        raise IndicatorsProviderError("BOJ workbook has no parseable date rows")
    return observations


def _value_column_index(provider_series_id: str) -> int:
    try:
        index = int(provider_series_id)
    except ValueError:
        raise IndicatorsProviderError(
            f"BOJ provider_series_id must be a 1-based column index: {provider_series_id!r}"
        ) from None
    if index < 2:
        raise IndicatorsProviderError(
            f"BOJ value column index must be a 1-based value column (>= 2): {index}"
        )
    return index


def _row_month(cells: Sequence[object]) -> date | None:
    for cell in cells:
        if isinstance(cell, datetime):
            return date(cell.year, cell.month, 1)
        if isinstance(cell, date):
            return date(cell.year, cell.month, 1)
    return None


def _cell_float(cell: object) -> float | None:
    if isinstance(cell, bool):
        return None
    if isinstance(cell, (int, float)):
        value = float(cell)
        return value if math.isfinite(value) else None
    return None
