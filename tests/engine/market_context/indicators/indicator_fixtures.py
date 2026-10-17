from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING, cast

import openpyxl
import requests
from tests.helpers.indicator_store import (
    series_definition,
)

from baibai_engine.collection.economic_providers import (
    parse_estat_json,
)
from baibai_engine.collection.economic_providers.base import (
    FetchContext,
)
from baibai_engine.collection.economic_providers.estat_dashboard import (
    parse_dashboard_json,
)
from baibai_engine.collection.economic_providers.spglobal_pmi import (
    Release,
    _parse_stream,
)
from baibai_engine.market_context.indicators.definitions import (
    SeriesDefinition,
)
from baibai_engine.market_context.indicators.observations import ObservationRecord
from baibai_engine.market_context.reading.estat import estat_values
from baibai_engine.market_context.reading.observations import reading_observation

if TYPE_CHECKING:
    # Only the annotation is needed; importing it for real would pull Playwright
    # onto the import path of the whole suite, which the lazy launch avoids.
    from baibai_engine.collection.economic_providers.browser import BrowserFetcher


def _multpl_monthly_table(*, floor: date, latest: date, omit: date | None = None) -> str:
    """Render a multpl by-month table covering ``floor``..``latest`` inclusive.

    Month arithmetic is spelled out here rather than reused from the provider so a
    defect in the provider's month stepping cannot cancel itself out in the fixture.
    """
    rows: list[str] = []
    month = floor
    while month <= latest:
        if month != omit:
            rows.append(f"<tr><td>{month:%b} 1, {month.year}</td><td>20.0</td></tr>")
        month = (
            date(month.year + 1, 1, 1)
            if month.month == 12
            else month.replace(month=month.month + 1)
        )
    return '<table id="datatable">' + "".join(reversed(rows)) + "</table>"


def _series(
    provider: str,
    provider_series_id: str,
    *,
    series_id: str = "test.series",
    unit: str = "percent",
    frequency: str = "daily",
    plausible_min: float | None = None,
    plausible_max: float | None = None,
) -> SeriesDefinition:
    """A provider-specific registry entry; `category` stays "test" for these tests."""

    return series_definition(
        series_id,
        provider=provider,
        provider_series_id=provider_series_id,
        category="test",
        unit=unit,
        frequency=frequency,
        plausible_min=plausible_min,
        plausible_max=plausible_max,
    )


def _estat_payload(
    value: object,
    *,
    status: int = 0,
    next_key: int | None = None,
) -> str:
    result_inf: dict[str, object] = {"TOTAL_NUMBER": 1}
    if next_key is not None:
        result_inf["NEXT_KEY"] = next_key
    return json.dumps(
        {
            "GET_STATS_DATA": {
                "RESULT": {"STATUS": status},
                "STATISTICAL_DATA": {
                    "RESULT_INF": result_inf,
                    "DATA_INF": {"VALUE": value},
                },
            }
        }
    )


def _registry_yaml(*, plausible_min: str, plausible_max: str) -> str:
    return (
        "series:\n"
        "  - series_id: test.series\n"
        "    name: Test series\n"
        "    category: rates\n"
        "    geography: test\n"
        "    frequency: daily\n"
        "    unit: percent\n"
        "    provider_series_id: TEST\n"
        "    source_id: fed-h15\n"
        f"    plausible_min: {plausible_min}\n"
        f"    plausible_max: {plausible_max}\n"
    )


def _pmi_stream(months: list[str]) -> tuple[Release, ...]:
    return _parse_stream(
        "jp_manufacturing",
        [
            {
                "observed_at": month,
                "url": (f"https://www.pmi.spglobal.com/Public/Home/PressRelease/{index:032x}"),
            }
            for index, month in enumerate(months, start=1)
        ],
    )


def _boj_workbook_bytes(
    rows: list[tuple[object, ...]],
    *,
    header_column: int = 3,
    header_label: str = "Monetary Base",
    metadata_column: int = 8,
    metadata: str = "Unit: 100 million yen",
    other_metadata: tuple[int, str] | None = None,
) -> bytes:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    header = [None] * header_column
    header[header_column - 1] = header_label
    worksheet.append(header)
    metadata_width = max(
        metadata_column,
        other_metadata[0] if other_metadata is not None else 0,
    )
    metadata_row: list[object] = [None] * metadata_width
    metadata_row[metadata_column - 1] = metadata
    if other_metadata is not None:
        metadata_row[other_metadata[0] - 1] = other_metadata[1]
    worksheet.append(metadata_row)
    for row in rows:
        worksheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


_DASHBOARD_INDICATOR = "0301010000020020010"


_DASHBOARD_SOURCE_URL = (
    "https://dashboard.e-stat.go.jp/api/1.0/Json/getData"
    f"?Lang=JP&IndicatorCode={_DASHBOARD_INDICATOR}"
    "&Cycle=1&IsSeasonalAdjustment=2&RegionCode=00000"
)


_DASHBOARD_SELECTORS = {
    "Lang": "JP",
    "IndicatorCode": _DASHBOARD_INDICATOR,
    "Cycle": "1",
    "IsSeasonalAdjustment": "2",
    "RegionCode": "00000",
}


def _dashboard_series(
    *,
    source_url: str = _DASHBOARD_SOURCE_URL,
    frequency: str = "monthly",
) -> SeriesDefinition:
    return replace(
        _series("estat_dashboard", _DASHBOARD_INDICATOR, frequency=frequency),
        source_url=source_url,
    )


def _dashboard_row(time_code: str, value: str) -> dict[str, dict[str, str]]:
    return {
        "VALUE": {
            "@indicator": _DASHBOARD_INDICATOR,
            "@unit": "001",
            "@stat": "00200531",
            "@regionCode": "00000",
            "@time": time_code,
            "@cycle": "1",
            "@regionRank": "2",
            "@isSeasonal": "2",
            "@isProvisional": "0",
            "$": value,
        }
    }


def _dashboard_payload(
    rows: Sequence[Mapping[str, Mapping[str, str]]],
    *,
    total: int | None = None,
) -> str:
    return json.dumps(
        {
            "GET_STATS": {
                "RESULT": {"status": "0", "errorMsg": "正常に終了しました。"},
                "STATISTICAL_DATA": {
                    "RESULT_INF": {"TOTAL_NUMBER": str(len(rows) if total is None else total)},
                    "TABLE_INF": {},
                    "DATA_INF": {"DATA_OBJ": list(rows)},
                },
            }
        }
    )


def _read_estat(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    records = parse_estat_json(series, text, start=start, end=end)
    return [
        reading_observation(series, observed_at=day, value=value, vintage_at=None)
        for day, value in estat_values(records).items()
    ]


def _parse_dashboard(
    rows: Sequence[Mapping[str, Mapping[str, str]]],
    *,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 12, 31),
) -> list[ObservationRecord]:
    records = parse_dashboard_json(
        _dashboard_payload(rows),
        start=start,
        end=end,
        selectors=_DASHBOARD_SELECTORS,
    )
    return [
        reading_observation(_dashboard_series(), observed_at=day, value=value, vintage_at=None)
        for day, value in estat_values(records, final_only=True).items()
    ]


class _ForbiddenResponse:
    """A 403 as an edge bot-mitigation serves it: a challenge page, not an error."""

    status_code = 403
    headers: dict[str, str] = {"Content-Type": "text/html"}

    def raise_for_status(self) -> None:
        raise requests.HTTPError("403 Client Error: Forbidden")

    def close(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        return [b"<!DOCTYPE html><html><title>Just a moment...</title></html>"]


class _FakeBrowserFetcher:
    """Stands in for the headless browser so a fallback is testable without Playwright.

    Returns whatever bytes it is given rather than only well-formed CSV, so a
    caller that skips validating the browser's answer is visible in a test.
    """

    def __init__(self, result: bytes | Exception) -> None:
        self.result = result
        self.urls: list[str] = []
        self.max_bytes: list[int] = []

    def fetch_download(self, url: str, *, max_bytes: int) -> bytes:
        self.urls.append(url)
        self.max_bytes.append(max_bytes)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def fetch_pdf(self, url: str, *, max_bytes: int) -> bytes:
        self.urls.append(url)
        self.max_bytes.append(max_bytes)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self) -> None:
        return None


def _context_with_browser(browser: _FakeBrowserFetcher) -> FetchContext:
    """A fetch context whose browser is already the fake, so none is ever launched."""

    context = FetchContext()
    context._browser = cast("BrowserFetcher", browser)
    return context


class _RecordingSession:
    """Captures the query a provider sends so the request contract is testable."""

    def __init__(self, body: str) -> None:
        self.body = body
        self.params: dict[str, str] | None = None

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: int,
        stream: bool = False,
    ) -> _FakeResponse:
        self.params = params
        return _FakeResponse(self.body.encode("utf-8"))


class _RaisingSession:
    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: int,
        stream: bool = False,
    ) -> object:
        raise requests.Timeout("simulated timeout")


class _StaticSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: int,
        stream: bool = False,
    ) -> _FakeResponse:
        return self.response


class _FakeResponse:
    def __init__(
        self,
        content: bytes,
        *,
        headers: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> None:
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        return [self.content]
