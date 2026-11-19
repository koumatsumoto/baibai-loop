"""Cutoff-bounded projection of one explicitly selected, retained Monex page."""

from __future__ import annotations

import hashlib
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Literal

from baibai_engine.data_collection.receipts.public_pages.models import EquityWebSourceResponse
from baibai_engine.data_collection.receipts.public_pages.receipts import (
    read_responses,
    validate_response,
)
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only

from .models import FrozenModel, utc_datetime
from .normalization import monex
from .normalization.decode import response_html
from .normalization.monex_eps import ReferenceEPS, reference_eps


class MonexEPSReceipt(FrozenModel):
    receipt_id: str
    attempt_id: str
    body_sha256: str
    ticker: str
    source_url: str
    received_at_utc: datetime
    distributor: Literal["monex"] = "monex"
    parser_version: Literal["monex-reference-eps-1"] = "monex-reference-eps-1"
    reference_eps: ReferenceEPS | None


def read_monex_eps(
    path: Path, *, receipt_id: str, ticker: str, cutoff: datetime
) -> MonexEPSReceipt:
    ticker, cutoff = normalize_ticker(ticker), utc_datetime(cutoff)
    with closing(connect_read_only(path)) as conn:
        conn.execute("BEGIN")
        row = conn.execute(
            "SELECT received_at_utc FROM equity_web_source_responses "
            "WHERE receipt_id=? AND source='monex' AND requested_ticker=? AND role='page'",
            (receipt_id, ticker),
        ).fetchone()
        if row is None or utc_datetime(datetime.fromisoformat(row[0])) > cutoff:
            raise ValueError("no matching retained Monex receipt at this cutoff")
        (receipt,) = read_responses(conn, (receipt_id,))
    return project_monex_eps(receipt, ticker=ticker, cutoff=cutoff)


def project_monex_eps(
    receipt: EquityWebSourceResponse, *, ticker: str, cutoff: datetime
) -> MonexEPSReceipt:
    """Project the exact object whose legacy observation the caller can independently verify."""
    validate_response(receipt)
    ticker, cutoff = normalize_ticker(ticker), utc_datetime(cutoff)
    if (
        receipt.source != "monex"
        or receipt.role != "page"
        or receipt.requested_ticker != ticker
        or receipt.received_at_utc > cutoff
    ):
        raise ValueError("no matching retained Monex receipt at this cutoff")
    page = monex.identified_page(
        response_html(receipt), ticker=ticker, source_url=receipt.requested_url
    )
    return MonexEPSReceipt(
        receipt_id=receipt.receipt_id,
        attempt_id=receipt.attempt_id,
        body_sha256=hashlib.sha256(receipt.body).hexdigest(),
        ticker=ticker,
        source_url=receipt.requested_url,
        received_at_utc=receipt.received_at_utc,
        reference_eps=reference_eps(page),
    )
