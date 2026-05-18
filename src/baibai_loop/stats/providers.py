from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, date, datetime
from typing import Protocol

import requests

from .db import ObservationRecord
from .definitions import SeriesDefinition

HTTP_TIMEOUT_SECONDS = 30
_H15_PACKAGE_SERIES = "bf17364827e38702b42a58cf8eaa3f78"


class StatsProviderError(RuntimeError):
    """Raised when a stats provider cannot return requested observations."""


class _Session(Protocol):
    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        timeout: int,
    ) -> requests.Response: ...


def fetch_observations(
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    session: _Session | None = None,
) -> list[ObservationRecord]:
    http = session or requests.Session()
    match series.provider:
        case "fred_csv":
            return _fetch_fred_csv(series, start=start, end=end, session=http)
        case "frb_h15":
            return _fetch_frb_h15(series, start=start, end=end, session=http)
        case "ecb_fx":
            return _fetch_ecb_fx(series, start=start, end=end, session=http)
        case _:
            raise StatsProviderError(f"unsupported stats provider: {series.provider}")


def parse_fred_csv(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or series.provider_series_id not in reader.fieldnames:
        raise StatsProviderError(f"FRED CSV missing column {series.provider_series_id}")
    observations: list[ObservationRecord] = []
    for row in reader:
        observed_at_raw = row.get("observation_date")
        value_raw = row.get(series.provider_series_id)
        if not observed_at_raw or not value_raw or value_raw == ".":
            continue
        observed_at = date.fromisoformat(observed_at_raw)
        if start <= observed_at <= end:
            observations.append(_record(series, observed_at=observed_at, value=_float(value_raw)))
    return observations


def parse_h15_csv(
    series: SeriesDefinition, text: str, *, start: date, end: date
) -> list[ObservationRecord]:
    lines = text.splitlines()
    header_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith(('"Time Period"', "Time Period,"))
        ),
        None,
    )
    if header_index is None:
        raise StatsProviderError("FRB H.15 CSV missing Time Period header")
    reader = csv.DictReader(lines[header_index:])
    observations: list[ObservationRecord] = []
    left_id, right_id = _split_provider_series_id(series.provider_series_id)
    for row in reader:
        observed_at_raw = row.get("Time Period")
        if not observed_at_raw:
            continue
        observed_at = date.fromisoformat(observed_at_raw)
        if not start <= observed_at <= end:
            continue
        left_value = _optional_float(row.get(left_id))
        if left_value is None:
            continue
        if right_id is None:
            value = left_value
        else:
            right_value = _optional_float(row.get(right_id))
            if right_value is None:
                continue
            value = (left_value - right_value) * 100.0
        observations.append(_record(series, observed_at=observed_at, value=value))
    return observations


def parse_ecb_fx_csv(
    series: SeriesDefinition,
    text: str,
    *,
    start: date,
    end: date,
) -> list[ObservationRecord]:
    reader = csv.DictReader(io.StringIO(text))
    observations: list[ObservationRecord] = []
    for row in reader:
        observed_at_raw = row.get("Date")
        if not observed_at_raw:
            continue
        observed_at = date.fromisoformat(observed_at_raw)
        if not start <= observed_at <= end:
            continue
        jpy = _optional_float(row.get("JPY"))
        if jpy is None:
            continue
        match series.provider_series_id:
            case "EURJPY":
                value = jpy
            case "USDJPY":
                usd = _optional_float(row.get("USD"))
                if usd is None or usd == 0:
                    continue
                value = jpy / usd
            case "AUDJPY":
                aud = _optional_float(row.get("AUD"))
                if aud is None or aud == 0:
                    continue
                value = jpy / aud
            case _:
                raise StatsProviderError(f"unsupported ECB FX series: {series.provider_series_id}")
        observations.append(_record(series, observed_at=observed_at, value=value))
    return observations


def _fetch_fred_csv(
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    session: _Session,
) -> list[ObservationRecord]:
    response = session.get(series.source_url, timeout=HTTP_TIMEOUT_SECONDS)
    _raise_for_response(response, series.source_url)
    return parse_fred_csv(series, response.text, start=start, end=end)


def _fetch_frb_h15(
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    session: _Session,
) -> list[ObservationRecord]:
    params = {
        "rel": "H15",
        "series": _H15_PACKAGE_SERIES,
        "lastObs": "",
        "from": start.strftime("%m/%d/%Y"),
        "to": end.strftime("%m/%d/%Y"),
        "filetype": "csv",
        "label": "include",
        "layout": "seriescolumn",
        "type": "package",
    }
    response = session.get(series.source_url, params=params, timeout=HTTP_TIMEOUT_SECONDS)
    _raise_for_response(response, series.source_url)
    return parse_h15_csv(series, response.text, start=start, end=end)


def _fetch_ecb_fx(
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    session: _Session,
) -> list[ObservationRecord]:
    response = session.get(series.source_url, timeout=HTTP_TIMEOUT_SECONDS)
    _raise_for_response(response, series.source_url)
    try:
        archive = zipfile.ZipFile(io.BytesIO(response.content))
    except zipfile.BadZipFile as exc:
        raise StatsProviderError("ECB FX response is not a ZIP archive") from exc
    csv_name = next((name for name in archive.namelist() if name.endswith(".csv")), None)
    if csv_name is None:
        raise StatsProviderError("ECB FX ZIP missing CSV file")
    text = archive.read(csv_name).decode("utf-8-sig")
    return parse_ecb_fx_csv(series, text, start=start, end=end)


def _raise_for_response(response: requests.Response, url: str) -> None:
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise StatsProviderError(f"failed to fetch {url}: {exc}") from exc


def _split_provider_series_id(provider_series_id: str) -> tuple[str, str | None]:
    if "-" not in provider_series_id:
        return provider_series_id, None
    left, right = provider_series_id.split("-", maxsplit=1)
    return left, right


def _optional_float(value: str | None) -> float | None:
    if value is None or not value or value in {"ND", "."}:
        return None
    return _float(value)


def _float(value: str) -> float:
    try:
        return float(value.replace(",", ""))
    except ValueError as exc:
        raise StatsProviderError(f"invalid numeric value: {value!r}") from exc


def _record(series: SeriesDefinition, *, observed_at: date, value: float) -> ObservationRecord:
    vintage_at = datetime.now(UTC)
    return ObservationRecord(
        series_id=series.series_id,
        observed_at=observed_at,
        period_start=observed_at,
        period_end=observed_at,
        value=value,
        unit=series.unit,
        vintage_at=vintage_at,
        fetch_status="ok",
        source_url=series.source_url,
    )
