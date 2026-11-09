"""Reconstruct ZIP planning evidence from one fixed list head, without market/L2 state."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from baibai_engine.data_collection.providers.edinet.archive_plan import holder_document_scope
from baibai_engine.data_collection.providers.edinet.document_rows import (
    document_inputs,
    native_document_index,
    projected_documents,
)
from baibai_engine.data_collection.providers.edinet.list_native import FixedEdinetLists
from baibai_engine.data_collection.providers.edinet.list_projection import project_document_list
from baibai_engine.data_collection.providers.edinet.models import EDINETProviderError
from baibai_engine.data_collection.providers.edinet.receipts import EdinetListReceipt


@dataclass(frozen=True, slots=True)
class ArchiveListInput:
    projected_documents: tuple[dict[str, Any], ...]
    native_rows: dict[tuple[str, int, str], dict[str, Any]]
    available_days: frozenset[date]
    failed_days: tuple[date, ...]

    def observed_documents(
        self, *, through: date, holder_only: bool = False
    ) -> list[dict[str, Any]]:
        documents = [
            doc for doc in self.projected_documents if str(doc["doc_date"]) <= through.isoformat()
        ]
        scoped = holder_document_scope(documents) if holder_only else documents
        return document_inputs(scoped, self.native_rows)

    def stored_documents(self, *, through: date) -> list[dict[str, Any]]:
        for day in self.failed_days:
            if day <= through and day not in self.available_days:
                raise EDINETProviderError(f"EDINET document coverage failed: {day.isoformat()}")
        return self.observed_documents(through=through)

    def require_complete_window(self, start: date, end: date) -> None:
        day = start
        while day <= end:
            if day not in self.available_days:
                raise EDINETProviderError("stored EDINET document list is unavailable")
            day += timedelta(days=1)


def read_archive_list_input(fixed: FixedEdinetLists) -> ArchiveListInput:
    by_day: dict[date, list[EdinetListReceipt]] = defaultdict(list)
    for item in fixed.iter_receipts():
        by_day[item.doc_date].append(item.decoded())
    documents: list[dict[str, Any]] = []
    native_rows: dict[tuple[str, int, str], dict[str, Any]] = {}
    available: set[date] = set()
    failed: list[date] = []
    for day in sorted(by_day):
        projection = project_document_list(by_day[day])
        if projection.error is not None:
            failed.append(day)
        if projection.selected_receipt is None:
            continue
        # A failed latest receipt remains reported. An eligible prior is the
        # same source evidence used by cold/warm list materialization.
        available.add(day)
        documents.extend(projected_documents(day.isoformat(), projection.documents))
        native_rows.update(
            {
                (day.isoformat(), *identity): row
                for identity, row in native_document_index(
                    projection.native_payload, doc_date=day.isoformat()
                ).items()
            }
        )
    documents.sort(key=lambda doc: (str(doc["doc_date"]), int(doc["seqNumber"])))
    return ArchiveListInput(tuple(documents), native_rows, frozenset(available), tuple(failed))
