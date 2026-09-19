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
    blocked = {event.ticker for event in quarantined if event.ticker}
    by_id = {str(doc["docID"]): doc for doc in origins}
    families: dict[str, list[dict[str, Any]]] = {}
    latest: dict[str, tuple[date, str, str]] = {}
    included: set[str] = set()
    unknown: dict[str, tuple[str, str]] = {}
    for doc in origins:
        if str(doc.get("docTypeCode")) not in {"120", "130"}:
            continue
        root = doc
        while str(root.get("docTypeCode")) == "130" and root.get("parentDocID") in by_id:
            root = by_id[root["parentDocID"]]
        if str(root.get("docTypeCode")) != "120":
            continue  # Missing ancestry is unknown, not a free-standing annual report.
        if not root.get("secCode"):
            continue
        ticker = parse_sec_code(root["secCode"])
        if ticker in blocked:
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
