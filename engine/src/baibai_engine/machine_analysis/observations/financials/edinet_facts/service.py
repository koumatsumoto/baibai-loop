"""Plan and extract annual research observations from retained XBRL archives."""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol, TextIO
from zipfile import BadZipFile

from baibai_engine.machine_analysis.observations.financials.edinet_decode import parse_sec_code
from baibai_engine.machine_analysis.observations.financials.edinet_facts.extract import (
    extract_facts,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.planning import (
    FactExtractionItem,
    plan_fact_item,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.selection import (
    annual_documents,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.store import (
    document_inventory,
    extraction_status,
    record_failure,
    record_initialized,
    store_facts,
)
from baibai_engine.machine_analysis.observations.financials.edinet_models import EDINETProviderError


class XbrlReader(Protocol):
    def read_xbrl_zip(self, doc_id: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class FactExtractionInputs:
    documents: list[dict[str, Any]]
    statuses: dict[str, tuple[str, str | None]]


def prepare_edinet_facts(
    *,
    asof: date,
    sqlite_path: Path,
    initialize: bool,
    tickers: tuple[str, ...],
    stdout: TextIO,
) -> FactExtractionInputs | None:
    statuses = extraction_status(sqlite_path)
    if not initialize and "initialized" not in statuses:
        print(
            "EDINET research facts: skipped=not_initialized; run explicit --initialize locally",
            file=stdout,
        )
        return None
    baseline = statuses.get("initialized")
    since = json.loads(baseline[1] or "{}").get("since") if baseline else None
    selected = annual_documents(
        document_inventory(sqlite_path, asof), retained=set(statuses), since=since
    )
    if tickers:
        selected = [doc for doc in selected if parse_sec_code(doc["secCode"]) in tickers]
    return FactExtractionInputs(selected, statuses)


def process_edinet_fact_item(
    *,
    item: FactExtractionItem,
    reader: XbrlReader,
    sqlite_path: Path,
    counts: Counter[str],
    reasons: Counter[str],
    stdout: TextIO,
) -> None:
    if item.action == "unknown_submission":
        counts["unknown_submission"] += 1
        return
    if item.action == "future":
        return
    identity = item.identity
    assert identity is not None
    if item.action == "unavailable":
        assert item.reason is not None
        counts["unavailable"] += 1
        record_failure(
            sqlite_path,
            doc_id=item.doc_id,
            disclosed_on=identity.disclosed_on,
            reason=item.reason,
        )
        return
    if item.action == "reused":
        counts["reused"] += 1
        return
    try:
        content = reader.read_xbrl_zip(item.doc_id)
        facts = extract_facts(
            content=content,
            ticker=identity.ticker,
            doc_id=item.doc_id,
            submitted=item.submitted,
        )
        store_facts(sqlite_path, identity=identity, facts=facts)
    except (EDINETProviderError, ValueError, BadZipFile, OSError) as exc:
        record_failure(
            sqlite_path,
            doc_id=item.doc_id,
            disclosed_on=identity.disclosed_on,
            reason=f"{type(exc).__name__}: {exc}",
        )
        counts["failed"] += 1
        return
    counts["extracted"] += 1
    counts["segments"] += len(facts.segments)
    counts["debt"] += len(facts.debt)
    reasons.update(facts.segment_reasons + facts.debt_reasons)
    if counts["extracted"] % 100 == 0:
        print(f"EDINET research facts progress: {dict(counts)}", file=stdout, flush=True)


def finish_edinet_facts(
    *,
    asof: date,
    sqlite_path: Path,
    initialize: bool,
    tickers: tuple[str, ...],
    counts: Counter[str],
    reasons: Counter[str],
    stdout: TextIO,
) -> int:
    if initialize and not tickers and counts["selected"] and not counts["failed"]:
        record_initialized(sqlite_path, asof)
    print(
        json.dumps(
            {"edinet_research_facts": dict(counts), "missing_reasons": dict(reasons)},
            ensure_ascii=False,
            sort_keys=True,
        ),
        file=stdout,
        flush=True,
    )
    return 1 if counts["failed"] or not counts["selected"] else 0


def extract_edinet_facts_command(
    *,
    asof: date,
    sqlite_path: Path,
    reader: XbrlReader,
    initialize: bool = False,
    tickers: tuple[str, ...] = (),
    stdout: TextIO | None = None,
) -> int:
    out = stdout if stdout is not None else sys.stdout
    inputs = prepare_edinet_facts(
        asof=asof, sqlite_path=sqlite_path, initialize=initialize, tickers=tickers, stdout=out
    )
    if inputs is None:
        return 0
    counts: Counter[str] = Counter(selected=len(inputs.documents))
    reasons: Counter[str] = Counter()
    for document in inputs.documents:
        item = plan_fact_item(document, statuses=inputs.statuses, asof=asof)
        process_edinet_fact_item(
            item=item,
            reader=reader,
            sqlite_path=sqlite_path,
            counts=counts,
            reasons=reasons,
            stdout=out,
        )
    return finish_edinet_facts(
        asof=asof,
        sqlite_path=sqlite_path,
        initialize=initialize,
        tickers=tickers,
        counts=counts,
        reasons=reasons,
        stdout=out,
    )
