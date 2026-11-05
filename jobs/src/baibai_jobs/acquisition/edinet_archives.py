"""Acquire retained EDINET originals; common integration extracts L2."""

from __future__ import annotations

import sqlite3
import sys
import time
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from baibai_engine.data_collection.providers.edinet.models import (
    EdinetAcquisitionError,
    EdinetRateLimitError,
)
from baibai_engine.data_collection.transport.http import AcquisitionDeadlineExceeded
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
from baibai_engine.machine_analysis.observations.financials.edinet_facts.planning import (
    plan_fact_item,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.service import (
    prepare_edinet_facts,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError
from baibai_engine.storage.sqlite.source_cache.coverage import date_covered
from baibai_engine.storage.sqlite.source_cache.read import connect_read_only
from baibai_engine.storage.sqlite.source_cache.schema import SQLiteSchemaError
from baibai_jobs.maintenance.source_refresh.edinet import EDINETRefresh

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


def metric_archive_candidates(database: Path, *, asof: date) -> tuple[list[str], int]:
    """Plan CSV originals from complete saved lists, never extracted metrics.

    The existing 540-day origin window and native event selection stay fixed.
    Older origins resolve edits/withdrawals but cannot become new candidates.
    """
    start = asof - timedelta(days=540)
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


def acquire_archives(
    database: Path,
    *,
    group: str,
    asof: date,
    api_key: str,
    retained: set[tuple[str, int]],
) -> dict[str, object]:
    """Read saved list/baseline inputs and archive identities, without extracting facts."""
    if group not in {"edinet-metrics", "edinet-facts"}:
        raise ValueError("unsupported EDINET archive group")
    facts_initialized = True
    if group == "edinet-metrics":
        try:
            candidates, metric_quarantined = metric_archive_candidates(database, asof=asof)
        except (sqlite3.Error, SQLiteSchemaError, EDINETProviderError, ValueError, OSError):
            return {"status": "failed", "failed": ["selection"]}
        documents = [(doc_id, 5) for doc_id in candidates]
    else:
        inputs = prepare_edinet_facts(
            asof=asof,
            sqlite_path=database,
            initialize=False,
            tickers=(),
            stdout=sys.stdout,
        )
        facts_initialized = inputs is not None
        documents = [
            (item.doc_id, 1)
            for document in (() if inputs is None else inputs.documents)
            if inputs is not None
            and (item := plan_fact_item(document, statuses=inputs.statuses, asof=asof)).action
            == "extract"
        ]
    holder_pending: list[str] = []
    holder_quarantined = 0
    holder_planning_failed = False
    if group == "edinet-facts":
        try:
            holder_pending, holder_quarantined = holder_archive_candidates(
                database, asof=asof, retained=retained
            )
        except (sqlite3.Error, EDINETProviderError, ValueError, OSError):
            # Preserve independent annual originals even if holder planning fails.
            holder_planning_failed = True
        documents.extend((doc_id, 1) for doc_id in holder_pending[:_HOLDER_ARCHIVES_PER_RUN])
    holder_selected = set(holder_pending[:_HOLDER_ARCHIVES_PER_RUN])
    downloaded, reused, failed = 0, 0, []
    with closing(EDINETRefresh(api_key, sqlite_path=database)) as acquisition:
        for doc_id, source_type in documents:
            if (doc_id, source_type) in retained:
                reused += 1
                continue
            try:
                if source_type == 5:
                    acquisition.download_csv_zip(doc_id)
                else:
                    acquisition.download_xbrl_zip(doc_id)
            except (
                AcquisitionDeadlineExceeded,
                EdinetAcquisitionError,
                EDINETProviderError,
                ValueError,
                OSError,
            ) as error:
                failed.append(doc_id)
                if isinstance(error, (AcquisitionDeadlineExceeded, EdinetRateLimitError)) or (
                    doc_id in holder_selected
                    and isinstance(error, EdinetAcquisitionError)
                    and str(error).endswith(("status 401", "status 403"))
                ):
                    break
            else:
                downloaded += 1
            if doc_id in holder_selected:
                time.sleep(_HOLDER_INTERVAL_SECONDS)
    report: dict[str, object] = {
        "status": "failed" if failed or holder_planning_failed else "received",
        "downloaded": downloaded,
        "retained_archives_reused": reused,
        "failed": failed,
    }
    if group == "edinet-metrics":
        report["document_events_quarantined"] = metric_quarantined
    if group == "edinet-facts":
        report.update(
            annual_archive_plan="available" if facts_initialized else "skipped_not_initialized",
            holder_planning_failed=holder_planning_failed,
            holder_not_selected_this_batch=max(0, len(holder_pending) - len(holder_selected)),
            document_events_quarantined=holder_quarantined,
        )
    return report
