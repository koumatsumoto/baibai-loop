from __future__ import annotations

import csv
import io
import zipfile
from datetime import date

from ..db import ObservationRecord
from ..definitions import SeriesDefinition
from .base import (
    MAX_ZIP_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    StatsProviderError,
    fetch_bytes,
    parse_optional_float,
    record_observation,
)

MAX_ECB_CSV_BYTES = 8_000_000
MAX_ZIP_COMPRESSION_RATIO = 100
ECB_FX_CSV_NAME = "eurofxref-hist.csv"


class EcbFxProvider:
    """ECB euro reference rates ZIP. JPY cross rates derived from a shared file."""

    name = "ecb_fx"

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
        try:
            archive = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc:
            raise StatsProviderError("ECB FX response is not a ZIP archive") from exc
        with archive:
            csv_infos = [
                info
                for info in archive.infolist()
                if not info.is_dir()
                and info.filename.rsplit("/", maxsplit=1)[-1] == ECB_FX_CSV_NAME
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
        jpy = parse_optional_float(row.get("JPY"))
        if jpy is None:
            continue
        match series.provider_series_id:
            case "EURJPY":
                value = jpy
            case "USDJPY":
                usd = parse_optional_float(row.get("USD"))
                if usd is None or usd == 0:
                    continue
                value = jpy / usd
            case "AUDJPY":
                aud = parse_optional_float(row.get("AUD"))
                if aud is None or aud == 0:
                    continue
                value = jpy / aud
            case _:
                raise StatsProviderError(f"unsupported ECB FX series: {series.provider_series_id}")
        observations.append(record_observation(series, observed_at=observed_at, value=value))
    return observations


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
