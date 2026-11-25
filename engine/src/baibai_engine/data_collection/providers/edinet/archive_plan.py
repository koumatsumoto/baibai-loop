"""Choose native ZIP originals from source document evidence and requested scope."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from baibai_engine.data_collection.providers.edinet.annual_filings import (
    annual_documents,
    usable_document,
)
from baibai_engine.data_collection.providers.edinet.facts_scope import FactsScope
from baibai_engine.data_collection.providers.edinet.fields import (
    filing_submitted_at,
    parse_sec_code,
)
from baibai_engine.data_collection.providers.edinet.models import EDINETProviderError
from baibai_engine.data_collection.providers.edinet.selection import (
    TARGET_DOC_TYPE_CODES,
    select_document_candidates,
)


def select_metric_archives(
    documents: Sequence[Mapping[str, Any]], *, origin_start: date
) -> tuple[list[str], int]:
    """Keep the CSV acquisition policy separate from L2 adoption/baseline policy."""
    selection = select_document_candidates(documents, origin_start=origin_start)
    if not selection.candidates:
        raise EDINETProviderError("no EDINET CSV filings selected in the acquisition window")
    quarantined = {
        event.ticker
        for event in selection.quarantined_events
        if event.ticker is not None and event.document_type in TARGET_DOC_TYPE_CODES
    }
    return (
        [
            candidate.doc_id
            for ticker, candidate in sorted(selection.candidates.items())
            if ticker not in quarantined
        ],
        len(selection.quarantined_events),
    )


@dataclass(frozen=True, slots=True)
class FactAcquisitionPlan:
    documents: list[dict[str, Any]] | None
    scope: FactsScope


def prepare_fact_acquisition(
    documents: list[dict[str, Any]],
    scope: FactsScope,
    *,
    initialize: bool = False,
    tickers: tuple[str, ...] = (),
) -> FactAcquisitionPlan:
    if scope.since is None and not initialize:
        return FactAcquisitionPlan(None, scope)
    selected = annual_documents(
        documents,
        retained=set(scope.doc_ids),
        since=scope.since.isoformat() if scope.since else None,
    )
    if tickers:
        selected = [doc for doc in selected if parse_sec_code(doc["secCode"]) in tickers]
    # Keep unavailable selected families requested before any HTTP or extraction.
    requested = FactsScope(doc_ids=frozenset(str(doc["docID"]) for doc in selected))
    return FactAcquisitionPlan(selected, scope.join(requested))


def needs_archive(document: dict[str, Any], *, asof: date) -> bool:
    try:
        parse_sec_code(document["secCode"])
        submitted = filing_submitted_at(str(document.get("submitDateTime") or ""))
    except ValueError:
        return False
    return submitted.date() <= asof and usable_document(document)
