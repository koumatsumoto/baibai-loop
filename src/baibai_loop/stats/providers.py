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
MAX_CSV_RESPONSE_BYTES = 8_000_000
MAX_ZIP_RESPONSE_BYTES = 16_000_000
MAX_ECB_CSV_BYTES = 8_000_000
MAX_ZIP_COMPRESSION_RATIO = 100
ECB_FX_CSV_NAME = "eurofxref-hist.csv"
_H15_PACKAGE_SERIES = "bf17364827e38702b42a58cf8eaa3f78"


class StatsProviderError(RuntimeError):
    """Raised when a stats provider cannot return requested observations."""


class FetchContext:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.bytes_cache: dict[tuple[str, tuple[tuple[str, str], ...]], bytes] = {}

    def close(self) -> None:
        self.session.close()


class _Session(Protocol):
    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        timeout: int,
        stream: bool = False,
    ) -> requests.Response: ...


def fetch_observations(
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    session: _Session | None = None,
    context: FetchContext | None = None,
) -> list[ObservationRecord]:
    if context is not None:
        return _fetch_observations_with_session(
            series,
            start=start,
            end=end,
            session=context.session,
            context=context,
        )
    if session is not None:
        return _fetch_observations_with_session(series, start=start, end=end, session=session)
    with requests.Session() as http:
        return _fetch_observations_with_session(series, start=start, end=end, session=http)


def _fetch_observations_with_session(
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    session: _Session,
    context: FetchContext | None = None,
) -> list[ObservationRecord]:
    match series.provider:
        case "fred_csv":
            return _fetch_fred_csv(series, start=start, end=end, session=session, context=context)
        case "frb_h15":
            return _fetch_frb_h15(series, start=start, end=end, session=session, context=context)
        case "ecb_fx":
            return _fetch_ecb_fx(series, start=start, end=end, session=session, context=context)
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
    fieldnames = set(reader.fieldnames or ())
    observations: list[ObservationRecord] = []
    left_id, right_id = _split_provider_series_id(series.provider_series_id)
    if left_id not in fieldnames:
        raise StatsProviderError(f"FRB H.15 CSV missing column {left_id}")
    if right_id is not None and right_id not in fieldnames:
        raise StatsProviderError(f"FRB H.15 CSV missing column {right_id}")
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
    fieldnames = set(reader.fieldnames or ())
    required_columns = _required_ecb_fx_columns(series.provider_series_id)
    missing_columns = required_columns - fieldnames
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise StatsProviderError(f"ECB FX CSV missing column(s): {missing}")
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
    context: FetchContext | None = None,
) -> list[ObservationRecord]:
    text = _get_text(
        session,
        series.source_url,
        params=None,
        max_bytes=MAX_CSV_RESPONSE_BYTES,
        context=context,
    )
    return parse_fred_csv(series, text, start=start, end=end)


def _fetch_frb_h15(
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    session: _Session,
    context: FetchContext | None = None,
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
    text = _get_text(
        session,
        series.source_url,
        params=params,
        max_bytes=MAX_CSV_RESPONSE_BYTES,
        context=context,
    )
    return parse_h15_csv(series, text, start=start, end=end)


def _fetch_ecb_fx(
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
    session: _Session,
    context: FetchContext | None = None,
) -> list[ObservationRecord]:
    content = _get_bytes(
        session,
        series.source_url,
        params=None,
        max_bytes=MAX_ZIP_RESPONSE_BYTES,
        context=context,
    )
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise StatsProviderError("ECB FX response is not a ZIP archive") from exc
    with archive:
        csv_infos = [
            info
            for info in archive.infolist()
            if not info.is_dir() and info.filename.rsplit("/", maxsplit=1)[-1] == ECB_FX_CSV_NAME
        ]
        if len(csv_infos) != 1:
            raise StatsProviderError(f"ECB FX ZIP must contain exactly one {ECB_FX_CSV_NAME}")
        info = csv_infos[0]
        if info.file_size > MAX_ECB_CSV_BYTES:
            raise StatsProviderError(f"ECB FX CSV too large: {info.file_size} bytes")
        if (
            info.compress_size
            and info.file_size / max(info.compress_size, 1) > MAX_ZIP_COMPRESSION_RATIO
        ):
            raise StatsProviderError("ECB FX ZIP compression ratio is too high")
        with archive.open(info) as handle:
            payload = handle.read(MAX_ECB_CSV_BYTES + 1)
        if len(payload) > MAX_ECB_CSV_BYTES:
            raise StatsProviderError(f"ECB FX CSV too large: {len(payload)} bytes")
    text = payload.decode("utf-8-sig")
    return parse_ecb_fx_csv(series, text, start=start, end=end)


def _get_text(
    session: _Session,
    url: str,
    *,
    params: dict[str, str] | None,
    max_bytes: int,
    context: FetchContext | None = None,
) -> str:
    content = _get_bytes(session, url, params=params, max_bytes=max_bytes, context=context)
    return content.decode("utf-8-sig")


def _get_bytes(
    session: _Session,
    url: str,
    *,
    params: dict[str, str] | None,
    max_bytes: int,
    context: FetchContext | None = None,
) -> bytes:
    key = (url, tuple(sorted((params or {}).items())))
    if context is not None and key in context.bytes_cache:
        return context.bytes_cache[key]
    try:
        response = session.get(url, params=params, timeout=HTTP_TIMEOUT_SECONDS, stream=True)
        _raise_for_response(response, url)
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                parsed_length = int(content_length)
            except ValueError:
                parsed_length = None
            if parsed_length is not None and parsed_length > max_bytes:
                raise StatsProviderError(f"stats response too large: {parsed_length} bytes")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise StatsProviderError(f"stats response too large: {total} bytes")
            chunks.append(chunk)
        content = b"".join(chunks)
    except requests.RequestException as exc:
        raise StatsProviderError(f"failed to fetch {url}: {exc}") from exc
    if context is not None:
        context.bytes_cache[key] = content
    return content


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


def _required_ecb_fx_columns(provider_series_id: str) -> set[str]:
    match provider_series_id:
        case "EURJPY":
            return {"Date", "JPY"}
        case "USDJPY":
            return {"Date", "JPY", "USD"}
        case "AUDJPY":
            return {"Date", "JPY", "AUD"}
        case _:
            raise StatsProviderError(f"unsupported ECB FX series: {provider_series_id}")


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
