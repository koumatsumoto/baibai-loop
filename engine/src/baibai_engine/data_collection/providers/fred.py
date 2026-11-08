from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from requests import RequestException, Session

from baibai_engine.data_collection.catalog import SourceDefinition
from baibai_engine.data_collection.provider_contracts import ProviderSpec
from baibai_engine.data_collection.receipts.native_sources.documents import SourceDocument
from baibai_engine.data_collection.receipts.native_sources.records import NativeRecords
from baibai_engine.data_collection.transport.context import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    ProviderFetchStoppedError,
    ProviderRateLimitError,
    fetch_bytes,
)
from baibai_engine.data_collection.transport.retry import mount_read_retry, read_deadline


class FredCsvResponseError(ProviderFetchStoppedError):
    """A received entity body could not become native records; retain that evidence."""

    def __init__(self, message: str, *, document: SourceDocument) -> None:
        super().__init__(message)
        self.document = document


class FredSource:
    """Retain FRED CSV cells, including missing markers, before L2 interpretation."""

    spec = ProviderSpec(
        name="fred_csv",
        all_history_start=date(1900, 1, 1),
    )
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
        try:
            if isinstance(session, Session):
                url = urlsplit(series.source_url)
                mount_read_retry(
                    session,
                    f"{url.scheme}://{url.netloc}/",
                    deadline=read_deadline(self.name, context),
                    max_bytes=MAX_CSV_RESPONSE_BYTES,
                )
            body = fetch_bytes(
                session,
                series.source_url,
                params=None,
                max_bytes=MAX_CSV_RESPONSE_BYTES,
                context=context,
            )
        except (IndicatorsProviderError, RequestException, ValueError) as exc:
            if context is not None:
                context.discard_cached_bytes(series.source_url, None)
            if isinstance(exc, ProviderRateLimitError):
                raise
            raise ProviderFetchStoppedError(str(exc)) from None
        try:
            return parse_fred_csv(series, body.decode("utf-8-sig"))
        except (IndicatorsProviderError, ValueError, csv.Error) as exc:
            if context is not None:
                context.discard_cached_bytes(series.source_url, None)
            # Successful native responses already retain their cells. Preserve
            # the entity body only where decoding/parsing would otherwise lose it.
            # Date/decoder exception text can embed source cells. Only the local
            # parser's fixed contract messages are safe for the failed-run log.
            message = (
                str(exc)
                if isinstance(exc, IndicatorsProviderError)
                else f"FRED CSV decode/parse failed: {type(exc).__name__}"
            )
            raise FredCsvResponseError(
                message,
                document=SourceDocument(
                    source_id=series.source_id,
                    request_key="history",
                    source_url=series.source_url,
                    content_type="text/csv",  # Expected format, not a captured HTTP header.
                    fetched_at=datetime.now(UTC),
                    payload=body,
                    metadata={
                        "provider": self.name,
                        "provider_series_id": series.provider_series_id,
                        "frequency": series.frequency,
                        "representation": "fred-csv-v1",
                    },
                ),
            ) from None


def parse_fred_csv(series: SourceDefinition, text: str) -> NativeRecords:
    reader = csv.DictReader(io.StringIO(text))
    for column in ("observation_date", series.provider_series_id):
        if column not in (reader.fieldnames or ()):
            raise IndicatorsProviderError(f"FRED CSV missing column {column}")
    columns = reader.fieldnames or []
    if len(columns) != len(set(columns)):
        raise IndicatorsProviderError("FRED CSV has duplicate columns")
    records: dict[date, list[dict[str, object]]] = defaultdict(list)
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise IndicatorsProviderError("FRED CSV row does not match its header")
        observed_at_raw = row.get("observation_date")
        if not observed_at_raw:
            raise IndicatorsProviderError("FRED CSV row has no observation_date")
        observed_at = date.fromisoformat(observed_at_raw)
        records[observed_at].append(dict(row))
    if not records:
        raise IndicatorsProviderError("FRED full-history CSV contains no observation rows")
    return dict(records)
