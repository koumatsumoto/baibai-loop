"""PMI readings from retained official PDFs or explicitly bound public articles."""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime

from pypdf import PdfReader

from baibai_engine.data_collection.providers.tradingeconomics.pmi_contract import (
    MONTHS,
    STREAM_NAMES,
    PmiArticle,
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
    """Select one receipt per observed month: official final, secondary final, then flash."""
    official: dict[date, SourceDocument] = {}
    secondary: list[tuple[date | None, SourceDocument]] = []
    for document in documents:
        if document.source_id != series.source_id:
            raise ValueError("PMI document is bound to a different measurement source")
        if document.content_type == "application/pdf":
            month = _document_month(document, "observed_at")
            if start <= month <= end and (
                month not in official or document.fetched_at > official[month].fetched_at
            ):
                official[month] = document
        elif document.content_type == "text/html":
            binding = document.metadata.get(
                "observed_at", document.metadata.get("requested_observed_at")
            )
            month_hint = date.fromisoformat(binding) if isinstance(binding, str) else None
            if month_hint is None or start <= month_hint <= end:
                secondary.append((month_hint, document))
        else:
            raise ValueError("PMI reading requires a retained PDF or bound TE HTML article")

    # A secondary article cannot supersede or invalidate a retained official release
    # for the same month. Official receipts do not assert a publication timestamp.
    values = {month: read_pmi_document(series, document) for month, document in official.items()}
    selected: dict[date, tuple[tuple[bool, datetime, datetime], SourceDocument, PmiArticle]] = {}
    for month_hint, document in secondary:
        if month_hint in official:
            continue
        article = parse_article(
            document.payload, stream=series.reading_key, url=document.source_url
        )
        month = article.observed_at
        if month_hint is not None and month != month_hint:
            raise ValueError("PMI article does not match its bound observation month")
        if not start <= month <= end or month in official:
            continue
        order = (article.phase == "final", article.published_at, document.fetched_at)
        if month not in selected or order > selected[month][0]:
            selected[month] = (order, document, article)

    for month, (_, document, article) in selected.items():
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
        values[month] = replace(
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
    return tuple(values[month] for month in sorted(values))
