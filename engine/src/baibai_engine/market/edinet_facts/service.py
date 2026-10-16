"""Collect annual research facts without changing the screening metric baseline."""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Protocol, TextIO
from zipfile import BadZipFile

from baibai_engine.market.edinet_decode import parse_sec_code
from baibai_engine.market.edinet_facts.extract import EXTRACTOR_REVISION, extract_facts
from baibai_engine.market.edinet_facts.models import filing_identity
from baibai_engine.market.edinet_facts.selection import annual_documents, usable_document
from baibai_engine.market.edinet_facts.store import (
    document_inventory,
    extraction_status,
    record_failure,
    record_initialized,
    store_facts,
)
from baibai_engine.market.edinet_models import EDINETProviderError, EDINETRateLimitError


class XbrlSource(Protocol):
    def download_xbrl_zip(self, doc_id: str) -> bytes: ...


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
