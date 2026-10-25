"""Synthetic public-response identities for private handoff tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from baibai_engine.job_api import EquityWebSourceResponse
from baibai_engine.data_collection.equity_web.models import Role, Source


def response(
    source: Source,
    ticker: str,
    body: bytes,
    *,
    attempt_id: str | None = None,
    role: Role = "page",
    parent_receipt_id: str | None = None,
    url: str | None = None,
    received_at_utc: datetime = datetime(2026, 10, 11, tzinfo=UTC),
    encoding: str = "utf-8",
) -> EquityWebSourceResponse:
    return EquityWebSourceResponse(
        receipt_id=str(uuid.uuid4()),
        attempt_id=attempt_id or str(uuid.uuid4()),
        source=source,
        requested_ticker=ticker,
        role=role,
        parent_receipt_id=parent_receipt_id,
        requested_url=url or f"https://synthetic.invalid/{source}/{ticker}/{role}",
        received_at_utc=received_at_utc,
        selected_encoding=encoding,
        body=body,
    )
