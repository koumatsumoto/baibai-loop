"""Collect annual research facts without changing the screening metric baseline."""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Protocol, TextIO
from zipfile import BadZipFile

from baibai_engine.market.edinet_facts.extract import EXTRACTOR_REVISION, extract_facts
from baibai_engine.market.edinet_facts.models import filing_identity
from baibai_engine.market.edinet_facts.store import (
    document_inventory,
    extraction_status,
    record_failure,
    record_initialized,
    store_facts,
)
from baibai_engine.screening.providers.edinet import (
    EDINETProviderError,
    EDINETRateLimitError,
    _canonicalize_document_events,
    _document_period,
    parse_sec_code,
)


class XbrlSource(Protocol):
    def download_xbrl_zip(self, doc_id: str) -> bytes: ...


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
    unresolved: dict[str, list[str]] = {}
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
        unresolved.setdefault(ticker, []).append(event_days.get(event.doc_id, ""))
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
        if str(root["docID"]) in unresolved_roots or any(
            not day or not root_submitted or day >= root_submitted
            for day in unresolved.get(ticker, ())
        ):
            continue
        doc = {**doc, "secCode": root["secCode"]}
        _, end = _document_period(root)
        root_id = str(root["docID"])
        doc["_research_period_known"] = end is not None
        if end is None:
            stamp = str(root.get("submitDateTime") or "")
            unknown[ticker] = max(unknown.get(ticker, ("", "")), (stamp, root_id))
        families.setdefault(root_id, []).append(doc)
        if str(doc["docID"]) in (retained or set()) or (
            since is not None and str(doc.get("submitDateTime") or "")[:10] > since
        ):
            included.add(root_id)
        rank = (end or date.min, str(root.get("submitDateTime") or ""), root_id)
        if ticker not in latest or rank > latest[ticker]:
            latest[ticker] = rank
    for ticker, (submitted, root_id) in unknown.items():
        if submitted >= latest[ticker][1]:
            latest[ticker] = (date.min, submitted, root_id)
    included.update(root_id for _, _, root_id in latest.values())
    return sorted(
        [doc for root_id in included for doc in families[root_id]],
        key=lambda doc: (str(doc.get("submitDateTime") or ""), str(doc["docID"])),
    )


def usable_document(doc: dict[str, Any]) -> bool:
    return (
        doc.get("_research_period_known", True)
        and str(doc.get("legalStatus")) in {"1", "2"}
        and str(doc.get("withdrawalStatus")) == "0"
        and str(doc.get("disclosureStatus")) == "0"
        and str(doc.get("docInfoEditStatus")) == "0"
        and str(doc.get("xbrlFlag")) == "1"
    )


def extract_edinet_facts_command(
    *,
    asof: date,
    sqlite_path: Path,
    provider: XbrlSource,
    initialize: bool = False,
    tickers: tuple[str, ...] = (),
    stdout: TextIO | None = None,
) -> int:
    out = stdout or sys.stdout
    statuses = extraction_status(sqlite_path)
    if not initialize and "initialized" not in statuses:
        print(
            "EDINET research facts: skipped=not_initialized; run explicit --initialize locally",
            file=out,
        )
        return 0
    baseline = statuses.get("initialized")
    since = json.loads(baseline[1] or "{}").get("since") if baseline else None
    selected = annual_documents(
        document_inventory(sqlite_path, asof), retained=set(statuses), since=since
    )
    if tickers:
        selected = [doc for doc in selected if parse_sec_code(doc["secCode"]) in tickers]
    counts: Counter[str] = Counter(selected=len(selected))
    reasons: Counter[str] = Counter()
    for doc in selected:
        doc_id = str(doc["docID"])
        submitted = str(doc.get("submitDateTime") or "")
        try:
            identity = filing_identity(parse_sec_code(doc["secCode"]), doc_id, submitted)
        except ValueError:
            counts["unknown_submission"] += 1
            continue
        if date.fromisoformat(identity.disclosed_on) > asof:
            continue
        if not usable_document(doc):
            counts["unavailable"] += 1
            record_failure(
                sqlite_path,
                doc_id=doc_id,
                disclosed_on=identity.disclosed_on,
                reason=(
                    "annual_period_unknown"
                    if not doc.get("_research_period_known", True)
                    else "document_validity_or_xbrl_unknown"
                ),
            )
            continue
        previous = statuses.get(doc_id)
        if (
            previous
            and previous[0] == "ok"
            and json.loads(previous[1] or "{}").get("revision") == EXTRACTOR_REVISION
        ):
            counts["reused"] += 1
            continue
        try:
            content = provider.download_xbrl_zip(doc_id)
            facts = extract_facts(
                content=content, ticker=identity.ticker, doc_id=doc_id, submitted=submitted
            )
            store_facts(sqlite_path, doc_id=doc_id, disclosed_on=identity.disclosed_on, facts=facts)
        except (EDINETProviderError, ValueError, BadZipFile, OSError) as exc:
            # Provider exception text is sanitized; source failures are fixed reason codes.
            record_failure(
                sqlite_path,
                doc_id=doc_id,
                disclosed_on=identity.disclosed_on,
                reason=f"{type(exc).__name__}: {exc}",
            )
            counts["failed"] += 1
            if isinstance(exc, EDINETRateLimitError):
                counts["rate_limited"] += 1
                break
            continue
        counts["extracted"] += 1
        counts["segments"] += len(facts.segments)
        counts["debt"] += len(facts.debt)
        reasons.update(facts.segment_reasons + facts.debt_reasons)
        if counts["extracted"] % 100 == 0:
            print(f"EDINET research facts progress: {dict(counts)}", file=out, flush=True)
    if initialize and not tickers and selected and not counts["failed"]:
        record_initialized(sqlite_path, asof)
    print(
        json.dumps(
            {"edinet_research_facts": dict(counts), "missing_reasons": dict(reasons)},
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=out,
        flush=True,
    )
    return 1 if counts["failed"] or not selected else 0
