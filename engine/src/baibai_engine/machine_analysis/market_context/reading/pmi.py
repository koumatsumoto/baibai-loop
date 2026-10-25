"""Deterministic PMI extraction from retained original PDF bodies."""

from __future__ import annotations

import io
from dataclasses import replace
from datetime import date

from pypdf import PdfReader

from baibai_engine.data_collection.economic.documents import SourceDocument
from baibai_engine.machine_analysis.market_context.indicators.definitions import SeriesDefinition
from baibai_engine.machine_analysis.market_context.indicators.observations import ObservationRecord

from .observations import reading_observation
from .pmi_extraction import PmiExtractionError, extract_pmi_value


def extract_pdf_text(content: bytes) -> str:
    if not content.startswith(b"%PDF"):
        raise ValueError("PMI source body is not a PDF")
    try:
        reader = PdfReader(io.BytesIO(content))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as error:
        raise ValueError(f"PMI source PDF could not be read: {error}") from error
    return " ".join(text.split())


def read_pmi_document(series: SeriesDefinition, document: SourceDocument) -> ObservationRecord:
    if document.source_id != series.source_id or document.content_type != "application/pdf":
        raise ValueError("PMI reading requires its bound source PDF")
    observed_at = _document_month(document, "observed_at")
    release_month = _document_month(document, "release_observed_at")
    if document.request_key != observed_at.isoformat():
        raise ValueError("PMI source request key does not match its requested month")
    text = extract_pdf_text(document.payload)
    try:
        value = extract_pmi_value(
            text, expected_observed_at=observed_at, release_observed_at=release_month
        )
    except PmiExtractionError as error:
        raise ValueError(f"{series.series_id} {observed_at}: {error}") from error
    if value is None:
        raise ValueError(
            f"{series.series_id} {observed_at}: no headline value found in {document.source_url}"
        )
    return replace(
        reading_observation(
            series, observed_at=observed_at, value=value, vintage_at=document.fetched_at
        ),
        source_url=document.source_url,
    )


def _document_month(document: SourceDocument, key: str) -> date:
    raw = document.metadata.get(key)
    if not isinstance(raw, str):
        raise ValueError(f"PMI source document is missing {key}")
    month = date.fromisoformat(raw)
    if month.day != 1:
        raise ValueError(f"PMI source document {key} must identify a month")
    return month
