"""Offline EDINET inputs and computed metrics; never acquire or create a store."""

from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.collection.edinet.archives import read_source_archive
from baibai_engine.market.edinet_document_events import parse_doc_id
from baibai_engine.market.edinet_metrics.store import read_edinet_documents, read_edinet_metrics
from baibai_engine.market.edinet_models import EdinetMetricRecord, EDINETProviderError
from baibai_engine.storage.observations.read import connect_read_only


class EDINETReader:
    def __init__(self, sqlite_path: Path) -> None:
        self._sqlite_path = sqlite_path

    def list_documents(self, on_date: date) -> list[dict[str, Any]]:
        records = read_edinet_documents(self._sqlite_path, on_date)
        if records is None:
            raise EDINETProviderError(
                f"stored EDINET document list is unavailable for {on_date.isoformat()}; "
                "acquire and project the source explicitly before reading"
            )
        return records

    def load_metric_records(self, asof_date: date) -> dict[str, EdinetMetricRecord]:
        records = read_edinet_metrics(self._sqlite_path, asof_date)
        return dict(records) if records is not None else {}

    def read_csv_zip(self, doc_id: str) -> bytes:
        return self._read_archive(doc_id, source_type=5)

    def read_xbrl_zip(self, doc_id: str) -> bytes:
        return self._read_archive(doc_id, source_type=1)

    def _read_archive(self, doc_id: str, *, source_type: int) -> bytes:
        safe_id = parse_doc_id(doc_id)
        content = None
        if self._sqlite_path.exists():
            with closing(connect_read_only(self._sqlite_path)) as connection:
                content = read_source_archive(connection, safe_id, source_type)
        if content is None:
            raise EDINETProviderError(
                f"stored EDINET archive is unavailable for {safe_id}, type {source_type}; "
                "acquire the source explicitly before reading"
            )
        return content
