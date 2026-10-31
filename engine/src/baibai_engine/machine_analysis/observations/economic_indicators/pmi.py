"""PMI readings from retained official PDFs or explicitly bound public articles."""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, date, datetime

from pypdf import PdfReader

from baibai_engine.data_collection.providers.tradingeconomics.pmi_contract import (
    MONTHS,
    STREAM_NAMES,
    parse_article,
)
from baibai_engine.data_collection.receipts.native_sources.documents import SourceDocument
from baibai_engine.machine_analysis.observations.economic_indicators.contracts import (
    SeriesDefinition,
)
from baibai_engine.machine_analysis.observations.economic_indicators.observations import (
    ObservationRecord,
)
from baibai_engine.machine_analysis.observations.economic_indicators.pmi_extraction import (
    PmiExtractionError,
    extract_pmi_value,
)
from baibai_engine.machine_analysis.observations.economic_indicators.validation import (
    reading_observation,
)


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


def read_pmi_documents(
    series: SeriesDefinition,
    documents: Iterable[SourceDocument],
    *,
    start: date,
    end: date,
) -> tuple[ObservationRecord, ...]:
    """One sample per month, final before flash regardless of arrival order."""
    selected: dict[date, tuple[tuple[int, datetime, datetime], SourceDocument]] = {}
    for document in documents:
        if document.content_type == "application/pdf":
            month = _document_month(document, "observed_at")
            phase = "final"
            # Legacy metadata has no source publication timestamp. Do not invent one.
            published = datetime.min.replace(tzinfo=UTC)
        elif document.content_type == "text/html":
            binding = document.metadata.get(
                "observed_at", document.metadata.get("requested_observed_at")
            )
            if isinstance(binding, str) and not start <= date.fromisoformat(binding) <= end:
                # A known older malformed receipt must not block another month's reading.
                continue
            article = parse_article(
                document.payload, stream=series.reading_key, url=document.source_url
            )
            month, phase, published = article.observed_at, article.phase, article.published_at
        else:
            raise ValueError("PMI reading requires a retained PDF or bound TE HTML article")
        if document.source_id != series.source_id:
            raise ValueError("PMI document is bound to a different measurement source")
        if not start <= month <= end:
            continue
        order = (1 if phase == "final" else 0, published, document.fetched_at)
        previous = selected.get(month)
        if previous is None or previous[0] < order:
            selected[month] = (order, document)
    values = []
    for month in sorted(selected):
        document = selected[month][1]
        if document.content_type == "application/pdf":
            values.append(read_pmi_document(series, document))
            continue
        article = parse_article(
            document.payload, stream=series.reading_key, url=document.source_url
        )
        # Select the current main-article level before its explicit target month.
        # Previous/forecast/flash comparisons and the appended stream are excluded.
        name = STREAM_NAMES[series.reading_key]
        match = re.search(
            rf"{name}\b(?P<statement>.*?)\b(?:in|for)\s+"
            rf"{MONTHS[month.month - 1]}\s+{month.year}\b",
            article.lead,
        )
        statement = match["statement"] if match else ""
        level = re.search(
            r"^\s*[®™]?\s*(?:(?:was\s+)?(?:confirmed|registered|stood|posted)\s+at|"
            r"(?:rose|fell|declined|climbed|eased|increased|decreased|"
            r"edged(?:\s+(?:up|down))?)\s+to)\s+"
            r"(?P<value>\d{1,3}(?:\.\d)?)(?![\d.])",
            statement,
        )
        if level is None:
            raise ValueError(f"{series.series_id}: no current headline level in TE main article")
        value = float(level["value"])
        if not 0 <= value <= 100:
            raise ValueError(f"{series.series_id}: PMI headline is outside the index range")
        values.append(
            replace(
                reading_observation(
                    series, observed_at=month, value=value, vintage_at=document.fetched_at
                ),
                source_url=document.source_url,
                quality_flags=("secondary_delivery", article.phase),
                notes=(
                    "measurement_provider=spglobal",
                    "delivery_provider=tradingeconomics",
                    "primary_unverified",
                    f"phase={article.phase}",
                    f"delivery_published_at={article.published_at.isoformat()}",
                    f"delivery_modified_at={article.modified_at.isoformat()}",
                    "S&P publication time is not established by the TE article timestamp",
                ),
            )
        )
    return tuple(values)
