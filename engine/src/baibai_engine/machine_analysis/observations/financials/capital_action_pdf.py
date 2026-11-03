"""Conservatively identify completed issued-share changes in retained JPX PDFs."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    disclosure_identity,
)
from baibai_engine.primitives.time import JST

if TYPE_CHECKING:
    import duckdb


@dataclass(frozen=True, slots=True)
class CapitalAction:
    ticker: str
    issued_shares: int
    effective_date: date
    received_at: datetime
    source_url: str
    source_sha256: str
    disclosure_number: str
    description: str


def extract_completed_conversion(
    content: bytes, *, source_url: str, received_at: datetime
) -> CapitalAction | None:
    """Extract only explicit completed same-day conversion totals, never plans.

    Other disclosure layouts are deliberately unclassified, rather than assuming
    coverage of all issuance events or estimating their effective dates.
    """
    ticker, disclosure_number = disclosure_identity(source_url)
    if received_at.utcoffset() is None:
        raise ValueError("capital action receipt requires a timezone")
    try:
        pages = PdfReader(io.BytesIO(content)).pages
        if not pages:
            return None
        texts = [unicodedata.normalize("NFKC", page.extract_text() or "") for page in pages]
    except (PdfReadError, ValueError):
        return None
    header = re.sub(r"\s+", "", texts[0])
    match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", header)
    if match is None or f"コード:{ticker}" not in header:
        return None
    published = date(*(int(part) for part in match.groups()))
    if published > received_at.astimezone(JST).date():
        return None
    text = re.sub(r"\s+", "", "".join(texts))
    if not re.search(r"本日、本株式交付の効力が発生(?:し|いたしました)", text):
        return None
    values = re.findall(r"本転換の結果、当(?:行|社)普通株式の発行済株式数は([\d,]+)株となり", text)
    if len(values) != 1:
        return None
    issued = int(values[0].replace(",", ""))
    if issued <= 0:
        return None
    return CapitalAction(
        ticker=ticker,
        issued_shares=issued,
        effective_date=published,
        received_at=received_at,
        source_url=source_url,
        source_sha256=hashlib.sha256(content).hexdigest(),
        disclosure_number=disclosure_number,
        description="completed_common_share_conversion_total",
    )


def read_capital_actions(
    connection: sqlite3.Connection | duckdb.DuckDBPyConnection,
    *,
    as_of: date,
    tickers: Sequence[str],
    known_at: datetime,
) -> dict[str, tuple[CapitalAction, ...]]:
    """Read only completed retained originals known by the generation cutoff."""
    if known_at.utcoffset() is None:
        raise ValueError("capital action cutoff requires timezone")
    selected = set(tickers)
    result: dict[str, list[CapitalAction]] = {}
    rows = connection.execute(
        "SELECT b.expected_resources_json, r.resource_name, r.requested_url, "
        "r.received_at_utc, r.content_type, r.body_gzip FROM jpx_source_bundles b "
        "JOIN jpx_source_responses r ON r.bundle_id=b.bundle_id "
        "WHERE b.collection_name='capital_actions' AND b.completed_at_utc IS NOT NULL "
        "ORDER BY r.received_at_utc, b.bundle_id, r.resource_name"
    )
    while batch := rows.fetchmany(8):
        for manifest, resource, url, received, media_type, compressed in batch:
            ticker, disclosure = disclosure_identity(str(url))
            if ticker not in selected:
                continue
            timestamp = datetime.fromisoformat(str(received))
            if timestamp.utcoffset() is None:
                raise ValueError("capital action receipt has no timezone")
            if timestamp > known_at:
                continue
            if (
                dict(json.loads(str(manifest))).get(resource) != url
                or resource != "pdf:" + disclosure
            ):
                raise ValueError("capital action original differs from source manifest")
            if str(media_type).split(";", 1)[0].strip().lower() != "application/pdf":
                raise ValueError("capital action PDF has an incompatible media type")
            action = extract_completed_conversion(
                gzip.decompress(bytes(compressed)), source_url=str(url), received_at=timestamp
            )
            if action is not None and action.effective_date <= as_of:
                result.setdefault(ticker, []).append(action)
    return {ticker: tuple(actions) for ticker, actions in result.items()}
