from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from datetime import date

from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.provider_contracts import ECB_FX_SPEC
from baibai_engine.data_collection.receipts.native_sources.records import NativeRecords
from baibai_engine.data_collection.transport.context import (
    MAX_ZIP_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    fetch_bytes,
)

MAX_ECB_CSV_BYTES = 8_000_000
MAX_ZIP_COMPRESSION_RATIO = 100
ECB_FX_CSV_NAME = "eurofxref-hist.csv"


class EcbFxSource:
    """Retain ECB euro-reference CSV fields without calculating currency crosses."""

    spec = ECB_FX_SPEC
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
            raise IndicatorsProviderError("ECB FX response is not a ZIP archive") from exc
        with archive:
            csv_infos = [
                info
                for info in archive.infolist()
                if not info.is_dir()
                and info.filename.rsplit("/", maxsplit=1)[-1] == ECB_FX_CSV_NAME
            ]
            if len(csv_infos) != 1:
                raise IndicatorsProviderError(
                    f"ECB FX ZIP must contain exactly one {ECB_FX_CSV_NAME}"
                )
            info = csv_infos[0]
            if info.file_size > MAX_ECB_CSV_BYTES:
                raise IndicatorsProviderError(f"ECB FX CSV too large: {info.file_size} bytes")
            if (
                info.compress_size
                and info.file_size / max(info.compress_size, 1) > MAX_ZIP_COMPRESSION_RATIO
            ):
                raise IndicatorsProviderError("ECB FX ZIP compression ratio is too high")
            with archive.open(info) as handle:
                payload = handle.read(MAX_ECB_CSV_BYTES + 1)
            if len(payload) > MAX_ECB_CSV_BYTES:
                raise IndicatorsProviderError(f"ECB FX CSV too large: {len(payload)} bytes")
        text = payload.decode("utf-8-sig")
        return parse_ecb_reference_csv(text, start=start, end=end)


def parse_ecb_reference_csv(text: str, *, start: date, end: date) -> NativeRecords:
    reader = csv.DictReader(io.StringIO(text))
    if "Date" not in (reader.fieldnames or ()):
        raise IndicatorsProviderError("ECB FX CSV missing Date column")
    if len(reader.fieldnames or ()) != len(set(reader.fieldnames or ())):
        raise IndicatorsProviderError("ECB FX CSV has duplicate columns")
    by_date: dict[date, list[dict[str, object]]] = defaultdict(list)
    for row in reader:
        if None in row:
            raise IndicatorsProviderError("ECB FX row has more fields than the header")
        try:
            observed_at = date.fromisoformat(row["Date"])
        except (TypeError, ValueError) as error:
            raise IndicatorsProviderError("ECB FX row has an invalid Date") from error
        if start <= observed_at <= end:
            by_date[observed_at].append(dict(row))
    return dict(by_date)
