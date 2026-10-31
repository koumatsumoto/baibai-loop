"""固定seal/lake共通の保存原本読取。履歴ごとの比較を返し、最新値へ集約しない。"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING

from baibai_engine.machine_analysis.observations.disclosures.edinet_document_events import (
    _canonicalize_document_events,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
    read_observed_documents,
)
from baibai_engine.machine_analysis.observations.financials.edinet_comparisons import (
    FilingComparisons,
    extract_comparisons,
)
from baibai_engine.machine_analysis.observations.financials.edinet_decode import parse_sec_code
from baibai_engine.machine_analysis.observations.financials.edinet_facts.models import (
    filing_identity,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.selection import (
    usable_document,
)

if TYPE_CHECKING:
    import duckdb


def read_comparison_population(
    connection: sqlite3.Connection | duckdb.DuckDBPyConnection,
    *,
    as_of: date,
    tickers: Sequence[str] | None = None,
    known_at: datetime | None = None,
) -> dict[str, tuple[FilingComparisons, ...]]:
    """Already received type=1 only. No missing ZIP can trigger a fetch or fallback.

    Every docID is a labelled vintage, including unsupported/missing inputs. Its
    current and comparative operands can only come from that same ZIP. Corrections
    remain separate vintages; they never rewrite an earlier document or J-Quants.
    """
    if known_at is not None and known_at.tzinfo is None:
        raise ValueError("comparison cutoff requires an aware datetime")
    documents, quarantined = _canonicalize_document_events(
        read_observed_documents(connection, through=as_of), origin_start=None
    )
    selected = set(tickers) if tickers is not None else None
    blocked_tickers = {
        event.ticker
        for event in quarantined
        if event.ticker is not None and event.document_type in {"120", "130", "160", "170"}
    }
    blocked_ids = {value for event in quarantined for value in (event.doc_id, event.target_doc_id)}
    observations: dict[str, FilingComparisons] = {}
    ready: dict[str, str] = {}
    retained_ids = {str(doc["docID"]) for doc in documents}
    for doc in documents:
        kind = str(doc.get("docTypeCode"))
        if kind not in {"120", "130", "160", "170"} or not doc.get("secCode"):
            continue
        ticker = parse_sec_code(doc["secCode"])
        if selected is not None and ticker not in selected:
            continue
        doc_id = str(doc["docID"])
        submitted = str(doc.get("submitDateTime") or "")
        try:
            identity = filing_identity(ticker, doc_id, submitted)
        except ValueError:
            continue  # No publication clock means no eligible operand.
        stamp = datetime.fromisoformat(identity.source_submit_datetime)
        if stamp.date() > as_of or (known_at is not None and stamp > known_at):
            continue
        reason = "comparison_archive_not_received"
        if ticker in blocked_tickers or doc_id in blocked_ids:
            reason = "comparison_document_events_unresolved"
        elif not usable_document(doc):
            reason = "comparison_document_unavailable"
        elif kind in {"130", "170"} and doc.get("parentDocID") not in retained_ids:
            reason = "comparison_correction_ancestry_unknown"
        else:
            ready[doc_id] = submitted
        observations[doc_id] = FilingComparisons(
            ticker=ticker,
            source_doc_id=doc_id,
            source_submit_datetime=stamp,
            document_type=kind,
            parent_doc_id=doc.get("parentDocID"),
            reason=reason,
        )
    received: set[str] = set()
    if observations:
        statement = "SELECT doc_id, zip_content FROM edinet_source_archives WHERE source_type = 1"
        parameters: tuple[str, ...] = ()
        if tickers is not None and len(tickers) == 1:
            # A company query must not materialize every issuer's native BLOBs.
            # Eligibility still comes from the folded, key-preserving observations
            # above; this is only an exact-docID restriction of the byte read.
            statement += (
                " AND doc_id IN (SELECT doc_id FROM edinet_documents WHERE sec_code = ?)"
            )
            parameters = (tickers[0] + "0",)
        cursor = connection.execute(statement, parameters)
        while batch := cursor.fetchmany(8):
            for doc_id, content in batch:
                if doc_id not in observations:
                    continue
                received.add(doc_id)
                base = observations[doc_id]
                if doc_id not in ready:
                    observations[doc_id] = base.model_copy(
                        update={"source_sha256": hashlib.sha256(bytes(content)).hexdigest()}
                    )
                    continue
                extracted = extract_comparisons(
                    content=bytes(content),
                    ticker=base.ticker,
                    doc_id=doc_id,
                    submitted=ready[doc_id],
                    document_type=base.document_type,
                )
                observations[doc_id] = extracted.model_copy(
                    update={"parent_doc_id": base.parent_doc_id}
                )
    # Absent unrelated archives are outside this optional capability. A missing
    # correction remains visible when its family has an actual archived input.
    neighbors: dict[str, set[str]] = {}
    for key, item in observations.items():
        if item.parent_doc_id in observations:
            parent = item.parent_doc_id
            assert parent is not None
            neighbors.setdefault(key, set()).add(parent)
            neighbors.setdefault(parent, set()).add(key)
    included = set(received)
    pending = list(received)
    while pending:
        key = pending.pop()
        for relative in neighbors.get(key, set()) - included:
            included.add(relative)
            pending.append(relative)
    population: dict[str, list[FilingComparisons]] = {}
    for item in sorted(
        (observations[key] for key in included),
        key=lambda item: (item.source_submit_datetime, item.source_doc_id),
    ):
        population.setdefault(item.ticker, []).append(item)
    return {ticker: tuple(items) for ticker, items in population.items()}
