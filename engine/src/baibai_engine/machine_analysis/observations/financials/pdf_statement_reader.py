"""固定原受領からPDF表示値を読む。HTTPも最新文書の推測も行わない。"""

from __future__ import annotations

import gzip
import json
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING

from baibai_engine.machine_analysis.observations.financials.pdf_statements import (
    PdfStatement,
    disclosure_identity,
    extract_pdf,
)

if TYPE_CHECKING:
    import duckdb


def read_pdf_statements(
    connection: sqlite3.Connection | duckdb.DuckDBPyConnection,
    *,
    as_of: date,
    tickers: Sequence[str] | None = None,
    known_at: datetime | None = None,
) -> dict[str, tuple[PdfStatement, ...]]:
    if known_at is not None and known_at.tzinfo is None:
        raise ValueError("PDF receipt cutoff requires timezone")
    selected = set(tickers) if tickers is not None else None
    rows = connection.execute(
        "SELECT b.bundle_id, b.expected_resources_json, r.resource_name, r.requested_url, "
        "r.received_at_utc, r.content_type, r.body_gzip FROM jpx_source_bundles b "
        "JOIN jpx_source_responses r ON r.bundle_id = b.bundle_id "
        "WHERE b.collection_name = 'financial_statements' AND b.completed_at_utc IS NOT NULL "
        "ORDER BY r.received_at_utc, b.bundle_id, r.resource_name"
    )
    population: dict[str, list[PdfStatement]] = {}
    while batch := rows.fetchmany(8):
        for bundle_id, manifest, resource, url, received, media_type, compressed in batch:
            ticker, disclosure = disclosure_identity(str(url))
            if selected is not None and ticker not in selected:
                continue
            stamp = datetime.fromisoformat(str(received))
            if stamp.tzinfo is None:
                raise ValueError("PDF receipt has no timezone")
            if known_at is not None and stamp > known_at:
                continue
            declared = dict(json.loads(str(manifest)))
            if declared.get(resource) != url or resource != "pdf:" + disclosure:
                raise ValueError("PDF original differs from its declared source bundle")
            if str(media_type).split(";", 1)[0].strip().lower() != "application/pdf":
                raise ValueError("financial PDF has an incompatible media type")
            observation = extract_pdf(
                content=gzip.decompress(bytes(compressed)),
                source_url=str(url),
                received_at=stamp,
                bundle_id=str(bundle_id),
                resource_name=str(resource),
            )
            if observation.published_on is None or observation.published_on <= as_of:
                population.setdefault(ticker, []).append(observation)
    return {ticker: tuple(items) for ticker, items in population.items()}
