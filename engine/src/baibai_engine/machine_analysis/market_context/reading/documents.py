"""L2 values from saved source documents; this module never acquires inputs."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date

from baibai_engine.data_collection.economic.documents import read_documents
from baibai_engine.machine_analysis.market_context.indicators.definitions import SeriesDefinition
from baibai_engine.machine_analysis.market_context.indicators.observations import ObservationRecord

from .matsui_margin import read_matsui_margin_documents
from .pmi import read_pmi_document
from .tsr_bankruptcies import parse_tsr_bankruptcies_json


def read_document_series(
    connection: sqlite3.Connection,
    series: SeriesDefinition,
    *,
    start: date,
    end: date,
) -> tuple[ObservationRecord, ...]:
    documents = read_documents(connection, series.source_id)
    if series.provider == "matsui_margin":
        return read_matsui_margin_documents(series, documents, start=start, end=end)
    if series.provider == "spglobal_pmi":
        return tuple(
            read_pmi_document(series, document)
            for document in documents
            if start <= date.fromisoformat(document.request_key) <= end
        )
    if series.provider != "tsr_bankruptcies":
        raise ValueError(f"unsupported document reading: {series.provider}")
    values: list[ObservationRecord] = []
    for document in documents:
        if document.request_key != "history" or document.content_type != "application/json":
            raise ValueError("TSR reading requires its complete publisher JSON")
        try:
            text = document.payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("TSR source JSON is not UTF-8") from error
        values.extend(
            replace(value, source_url=document.source_url)
            for value in parse_tsr_bankruptcies_json(
                series, text, start=start, end=end, vintage_at=document.fetched_at
            )
        )
    return tuple(values)
