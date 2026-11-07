"""保存一覧からnative取得用のCSV/holder原本候補を選ぶ。"""

from __future__ import annotations

from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.machine_analysis.observations.disclosures.edinet_document_events import (
    _canonicalize_document_events,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
    read_observed_documents,
    read_stored_documents,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_selection import (
    TARGET_DOC_TYPE_CODES,
    select_document_candidates,
)
from baibai_engine.machine_analysis.observations.financials.edinet_decode import (
    is_usable_filing_status,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.storage.sqlite.source_cache.coverage import date_covered
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only

# Spread the backfill across existing daily runs; the retained IDs are the queue.
# These are public disclosure originals, not a claim that every filing contains
# an investment-relevant share change or is attributable to a known ticker.
_HOLDER_ARCHIVES_PER_RUN = 100
_HOLDER_INTERVAL_SECONDS = 10


def holder_archive_candidates(
    database: Path, *, asof: date, retained: set[tuple[str, int]]
) -> tuple[list[str], int]:
    with closing(connect_read_only(database)) as connection:
        connection.execute("BEGIN")
        documents = read_observed_documents(connection, through=asof, holder_only=True)
    origins, quarantined = _canonicalize_document_events(documents, origin_start=None)
    candidates = {
        str(doc["docID"]): str(doc.get("submitDateTime") or "")
        for doc in origins
        if str(doc.get("docTypeCode")) in {"350", "360"}
        and str(doc.get("xbrlFlag")) == "1"
        and (str(doc.get("docID")), 1) not in retained
        and is_usable_filing_status(
            doc.get("legalStatus"), doc.get("disclosureStatus"), doc.get("withdrawalStatus")
        )
    }
    ordered = sorted(candidates, key=lambda doc_id: (candidates[doc_id], doc_id), reverse=True)
    return ordered, len(quarantined)


def metric_archive_candidates(
    database: Path, *, asof: date, lookback_days: int = 540
) -> tuple[list[str], int]:
    """Plan CSV originals from complete saved lists, never extracted metrics.

    The requested origin window and native event selection stay fixed.
    Older origins resolve edits/withdrawals but cannot become new candidates.
    """
    start = asof - timedelta(days=lookback_days)
    with closing(connect_read_only(database)) as connection:
        connection.execute("BEGIN")
        day = start
        while day <= asof:
            if not date_covered(connection, "edinet_documents", day):
                raise EDINETProviderError("stored EDINET document list is unavailable")
            day += timedelta(days=1)
        documents = read_stored_documents(connection, through=asof, start=start)
        documents.extend(read_observed_documents(connection, through=start - timedelta(days=1)))
    selection = select_document_candidates(documents, origin_start=start)
    if not selection.candidates:
        raise EDINETProviderError("no EDINET CSV filings selected in the acquisition window")
    # An absent native origin cannot gain an issuer identity from an L2 metric.
    # Extraction still owns its baseline-only quarantine and adoption decision.
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
