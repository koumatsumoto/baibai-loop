"""Acquire the publisher's complete bankruptcy JSON, before value extraction."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from baibai_engine.data_collection.receipts.native_sources.documents import SourceDocument
from baibai_engine.data_collection.provider_contracts import ProviderSpec
from baibai_engine.data_collection.catalog import SourceDefinition

from baibai_engine.data_collection.transport.context import (
    MAX_CSV_RESPONSE_BYTES,
    FetchContext,
    HttpSession,
    IndicatorsProviderError,
    fetch_bytes,
)


class TsrBankruptciesSource:
    spec = ProviderSpec(name="tsr_bankruptcies", all_history_start=date(2003, 1, 1))
    name = spec.name

    def fetch_document(
        self,
        source: SourceDefinition,
        *,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> SourceDocument:
        body = fetch_bytes(
            session,
            source.source_url,
            params={"data": "data"},
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
        )
        try:
            json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise IndicatorsProviderError("TSR response is not UTF-8 JSON") from error
        return SourceDocument(
            source_id=source.source_id,
            request_key="history",
            source_url=source.source_url,
            content_type="application/json",
            fetched_at=datetime.now(UTC),
            payload=body,
            metadata={"params": {"data": "data"}},
        )
