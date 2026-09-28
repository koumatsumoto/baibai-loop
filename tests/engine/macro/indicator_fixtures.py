from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import openpyxl
import requests
from tests.helpers.indicator_store import (
    series_definition,
)

import baibai_engine.macro.indicators.db as indicators_db
from baibai_engine.macro.indicators.db import (
    ObservationRecord,
    get_series,
    initialize_database,
    insert_observations,
    record_provider_run,
)
from baibai_engine.macro.indicators.definitions import (
    SeriesDefinition,
    load_definitions,
)
from baibai_engine.macro.indicators.providers import (
    spglobal_pmi,
)
from baibai_engine.macro.indicators.providers.base import (
    FetchContext,
    HttpSession,
)
from baibai_engine.macro.indicators.providers.estat_dashboard import (
    parse_dashboard_json,
)
from baibai_engine.macro.indicators.providers.spglobal_pmi import (
    Release,
    SpGlobalPmiProvider,
    _parse_stream,
)

if TYPE_CHECKING:
    # Only the annotation is needed; importing it for real would pull Playwright
    # onto the import path of the whole suite, which the lazy launch avoids.
    from baibai_engine.macro.indicators.providers.browser import BrowserFetcher


def _prune_store_rows(database: Path) -> dict[str, tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(database) as connection:
        return {
            table: tuple(sorted(connection.execute(f"SELECT * FROM {table}").fetchall(), key=repr))
            for table in ("series", "aliases", "observations", "provider_runs", "registry_state")
        }


def _write_retired_series(database: Path) -> None:
    """A store written while the series was still registered, then left behind.

    The generation is one behind the current registry because retiring a series
    changes the canonical membership, and only a client whose registry is newer
    than the store may prune what the store still holds.
    """

    conn = initialize_database(database)
    try:
        indicators_db.set_registry_generation(conn, load_definitions().generation - 1)
        conn.execute(
            "INSERT INTO series("
            "series_id, name, category, geography, frequency, unit, provider, "
            "provider_series_id, source_id, source_url"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "jp.cpi.stale",
                "stale",
                "inflation",
                "japan",
                "monthly",
                "index",
                "fred_csv",
                "JPNCPIALLMINMEI",
                "fred-jp-cpi",
                "https://example.com/stale.csv",
            ),
        )
        conn.execute(
            "INSERT INTO aliases(alias, series_id) VALUES (?, ?)",
            ("stale alias", "jp.cpi.stale"),
        )
        insert_observations(
            conn,
            [
                ObservationRecord(
                    series_id="jp.cpi.stale",
                    observed_at=date(2021, 12, 1),
                    value=100.0,
                    unit="index",
                    source_url="https://example.com/stale.csv",
                    vintage_at=datetime(2026, 5, 1, tzinfo=UTC),
                )
            ],
        )
        record_provider_run(
            conn,
            provider="fred_csv",
            series_id="jp.cpi.stale",
            start=date(2021, 1, 1),
            end=date(2021, 12, 31),
            started_at=datetime(2026, 5, 1, tzinfo=UTC),
            status="ok",
            record_count=1,
        )
        conn.commit()
    finally:
        conn.close()


def _retired_counts(database: Path) -> tuple[int, int, int]:
    conn = sqlite3.connect(database)
    try:
        return cast(
            tuple[int, int, int],
            tuple(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE series_id = ?",
                    ("jp.cpi.stale",),
                ).fetchone()[0]
                for table in ("series", "observations", "provider_runs")
            ),
        )
    finally:
        conn.close()


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
        "    provider: fred_csv\n"
        "    provider_series_id: TEST\n"
        "    source_id: test-source\n"
        "    source_url: https://example.com/test.csv\n"
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


def _pmi_stored(
    series_id: str, releases: Sequence[Release], values: Sequence[float]
) -> tuple[ObservationRecord, ...]:
    """Stored observations carrying the release URL their month names in the manifest."""
    return tuple(
        ObservationRecord(
            series_id=series_id,
            observed_at=release.observed_at,
            value=value,
            unit="index",
            source_url=release.url,
            vintage_at=datetime.now(UTC),
        )
        for release, value in zip(releases, values, strict=True)
    )


def _fetch_pmi_stream(
    series: SeriesDefinition,
    stream: tuple[Release, ...],
    *,
    start: date,
    end: date,
    context: FetchContext | None,
) -> tuple[list[ObservationRecord], list[str]]:
    """Run the PMI provider against a fixture manifest, recording what it fetched.

    Release text is stubbed per URL so the test stays on the fetch-selection and
    manifest-freshness behaviour instead of PDF parsing.
    """

    month_by_url = {release.url: release.observed_at for release in stream}
    fetched: list[str] = []

    def release_text(url: str, *, session: object, context: object) -> str:
        fetched.append(url)
        return f"the headline PMI posted 50.4 in {month_by_url[url].strftime('%B')}."

    with (
        patch.object(spglobal_pmi, "load_manifest", return_value={"jp_manufacturing": stream}),
        patch.object(spglobal_pmi, "release_text", side_effect=release_text),
    ):
        observations = SpGlobalPmiProvider().fetch(
            series,
            start=start,
            end=end,
            session=cast(HttpSession, _RaisingSession()),
            context=context,
        )
    return observations, fetched


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


def _write_observation_with_coverage(
    db: Path,
    series_id: str,
    *,
    observed_at: date,
    value: float,
    coverage_start: date,
    coverage_end: date,
) -> None:
    _write_observation(db, series_id, observed_at=observed_at, value=value)
    conn = sqlite3.connect(db)
    try:
        record_provider_run(
            conn,
            provider="test",
            series_id=series_id,
            start=coverage_start,
            end=coverage_end,
            started_at=datetime.now(UTC),
            status="ok",
            record_count=1,
        )
        conn.commit()
    finally:
        conn.close()


def _write_observation(
    db: Path,
    series_id: str,
    *,
    observed_at: date,
    value: float,
    vintage_at: datetime | None = None,
) -> None:
    conn = initialize_database(db)
    try:
        series = get_series(conn, series_id)
        insert_observations(
            conn,
            [
                ObservationRecord(
                    series_id=series_id,
                    observed_at=observed_at,
                    value=value,
                    unit=series.unit,
                    source_url=series.source_url,
                    vintage_at=vintage_at or datetime.now(UTC),
                )
            ],
        )
        conn.commit()
    finally:
        conn.close()


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


def _parse_dashboard(
    rows: Sequence[Mapping[str, Mapping[str, str]]],
    *,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 12, 31),
) -> list[ObservationRecord]:
    return parse_dashboard_json(
        _dashboard_series(),
        _dashboard_payload(rows),
        start=start,
        end=end,
        selectors=_DASHBOARD_SELECTORS,
    )


class _ForbiddenResponse:
    """A 403 as an edge bot-mitigation serves it: a challenge page, not an error."""

    status_code = 403
    headers: dict[str, str] = {"Content-Type": "text/html"}

    def raise_for_status(self) -> None:
        raise requests.HTTPError("403 Client Error: Forbidden")

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

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int) -> list[bytes]:
        return [self.content]
