"""Receive one current Matsui market page, without extracting L2 values."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.economic.documents import SourceDocument
from baibai_engine.data_collection.economic.provider_specs import ProviderSpec
from baibai_engine.data_collection.economic.sources import SourceDefinition

from .base import MAX_CSV_RESPONSE_BYTES, FetchContext, HttpSession, fetch_bytes


class MatsuiMarginSource:
    # The public current page is not a historical archive.
    spec = ProviderSpec(name="matsui_margin")
    name = spec.name

    def fetch_document(
        self,
        source: SourceDefinition,
        *,
        session: HttpSession,
        context: FetchContext | None = None,
    ) -> SourceDocument:
        # Document acquisition has no Collection retry loop, browser fallback or
        # date traversal. A single ordinary GET preserves the complete HTML,
        # including its original labels and qualifications even if L2 cannot read it.
        body = fetch_bytes(
            session,
            source.source_url,
            params=None,
            max_bytes=MAX_CSV_RESPONSE_BYTES,
            context=context,
        )
        received = datetime.now(UTC)
        receipt_day = received.astimezone(ZoneInfo("Asia/Tokyo")).date().isoformat()
        return SourceDocument(
            source_id=source.source_id,
            request_key=receipt_day,
            source_url=source.source_url,
            content_type="text/html",
            fetched_at=received,
            payload=body,
            metadata={"receipt_jst_date": receipt_day},
        )
