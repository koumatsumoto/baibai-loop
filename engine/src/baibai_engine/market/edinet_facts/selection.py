"""Pure annual-filing selection shared by extraction and saved research reads."""

from __future__ import annotations

from datetime import date
from typing import Any

from baibai_engine.market.edinet_decode import (
    document_period,
    is_usable_filing_status,
    parse_sec_code,
)
from baibai_engine.market.edinet_document_events import _canonicalize_document_events


def annual_documents(
    documents: list[dict[str, Any]], *, retained: set[str] | None = None, since: str | None = None
) -> list[dict[str, Any]]:
    """Latest annual family per issuer, including every linked correction.

    Select the family before checking validity or XBRL availability. An invalid or
    unsupported latest filing must not make an older annual report look current.
    """
    origins, quarantined = _canonicalize_document_events(documents, origin_start=None)
    by_id = {str(doc["docID"]): doc for doc in origins}
    annual_ids = {
        key for key, doc in by_id.items() if str(doc.get("docTypeCode")) in {"120", "130"}
    }
    roots: dict[str, dict[str, Any]] = {}
    for key in annual_ids:
        ancestor = by_id[key]
        while str(ancestor.get("docTypeCode")) == "130" and ancestor.get("parentDocID") in by_id:
            ancestor = by_id[ancestor["parentDocID"]]
        if str(ancestor.get("docTypeCode")) == "120":
            roots[key] = ancestor
    event_days: dict[str, str] = {}
    event_parents: dict[str, str] = {}
    for doc in documents:
        key = str(doc.get("docID") or "")
        if doc.get("parentDocID"):
            event_parents[key] = str(doc["parentDocID"])
        event_days[key] = max(
            event_days.get(key, ""),
            str(doc.get("doc_date") or ""),
            str(doc.get("submitDateTime") or "")[:10],
        )
    unresolved_roots: set[str] = set()
    unresolved: dict[str, list[tuple[str, date | None]]] = {}
    # A correction without its annual ancestry cannot silently leave an older
    # filing usable. A known earlier fiscal period cannot invalidate a newer one.
    for key in annual_ids - roots.keys():
        doc = by_id[key]
        if doc.get("secCode"):
            orphan_ticker = parse_sec_code(doc["secCode"])
            _, orphan_end = document_period(doc)
            unresolved.setdefault(orphan_ticker, []).append((event_days.get(key, ""), orphan_end))
    for event in quarantined:
        linked = [
            by_id[key]
            for key in (event.doc_id, event.target_doc_id, event_parents.get(event.doc_id))
            if key in annual_ids
        ]
        linked_roots = {
            str(roots[str(doc["docID"])]["docID"]) for doc in linked if str(doc["docID"]) in roots
        }
        if linked_roots:
            unresolved_roots.update(linked_roots)
            continue
        if event.document_type not in {"120", "130"}:
            continue
        ticker = event.ticker or next(
            (parse_sec_code(doc["secCode"]) for doc in linked if doc.get("secCode")), None
        )
        if ticker is None:
            continue
        unresolved.setdefault(ticker, []).append((event_days.get(event.doc_id, ""), None))
    families: dict[str, list[dict[str, Any]]] = {}
    latest: dict[str, tuple[date, str, str]] = {}
    included: set[str] = set()
    unknown: dict[str, tuple[str, str]] = {}
    for doc in origins:
        if str(doc.get("docTypeCode")) not in {"120", "130"}:
            continue
        root = roots.get(str(doc["docID"]))
        if root is None:
            continue  # Missing ancestry is unknown, not a free-standing annual report.
        if not root.get("secCode"):
            continue
        ticker = parse_sec_code(root["secCode"])
        root_submitted = str(root.get("submitDateTime") or "")[:10]
        _, end = document_period(root)
        root_id = str(root["docID"])
        rank = (end or date.min, str(root.get("submitDateTime") or ""), root_id)
        if ticker not in latest or rank > latest[ticker]:
            latest[ticker] = rank
        if end is None:
            stamp = str(root.get("submitDateTime") or "")
            unknown[ticker] = max(unknown.get(ticker, ("", "")), (stamp, root_id))
        if str(root["docID"]) in unresolved_roots or any(
            (orphan_end is None or end is None or orphan_end >= end)
            and (not day or not root_submitted or day >= root_submitted)
            for day, orphan_end in unresolved.get(ticker, ())
        ):
            continue
        doc = {**doc, "secCode": root["secCode"]}
        doc["_research_period_known"] = end is not None
        doc["_research_annual_period_end"] = end.isoformat() if end is not None else None
        families.setdefault(root_id, []).append(doc)
        if str(doc["docID"]) in (retained or set()) or (
            since is not None and str(doc.get("submitDateTime") or "")[:10] > since
        ):
            included.add(root_id)
    for ticker, (submitted, root_id) in unknown.items():
        if submitted >= latest[ticker][1]:
            latest[ticker] = (date.min, submitted, root_id)
    included.update(root_id for _, _, root_id in latest.values() if root_id in families)
    return sorted(
        [doc for root_id in included for doc in families[root_id]],
        key=lambda doc: (str(doc.get("submitDateTime") or ""), str(doc["docID"])),
    )


def usable_document(doc: dict[str, Any]) -> bool:
    return (
        doc.get("_research_period_known", True)
        and is_usable_filing_status(
            doc.get("legalStatus"), doc.get("disclosureStatus"), doc.get("withdrawalStatus")
        )
        and str(doc.get("docInfoEditStatus")) in {"0", "2"}
        and str(doc.get("xbrlFlag")) == "1"
    )


def latest_annual_filing(documents: list[dict[str, Any]], ticker: str) -> dict[str, Any] | None:
    """Select the latest revision before checking usability; never fall back for facts.

    The caller supplies all retained documents through its explicit cutoff. No
    result can mean unresolved ancestry or events, not that the issuer has no filing.
    """
    filings = [
        doc for doc in annual_documents(documents) if parse_sec_code(doc["secCode"]) == ticker
    ]
    return max(
        filings,
        key=lambda doc: (str(doc.get("submitDateTime") or ""), str(doc["docID"])),
        default=None,
    )
