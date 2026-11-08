"""固定seal/lake共通の保存原本読取。履歴ごとの比較を返し、最新値へ集約しない。"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from collections.abc import Collection, Iterable, Iterator, Sequence
from datetime import date, datetime
from typing import TYPE_CHECKING, TextIO

from pydantic import JsonValue

from baibai_engine.data_collection.providers.edinet.archive_native import FixedEdinetArchives
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_events import (
    _canonicalize_document_events,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
    read_observed_documents,
)
from baibai_engine.machine_analysis.observations.financials.edinet_comparisons import (
    FilingComparisons,
    extract_comparisons,
    successful_comparison_seed,
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
    archives: FixedEdinetArchives | None = None,
    prior_observations: Iterable[dict[str, JsonValue]] = (),
    diagnostics: TextIO | None = None,
) -> dict[str, tuple[FilingComparisons, ...]]:
    """Already received type=1 only. No missing ZIP can trigger a fetch or fallback.

    Every docID is a labelled vintage, including unsupported/missing inputs. Its
    current and comparative operands can only come from that same ZIP. Corrections
    remain separate vintages; they never rewrite an earlier document or J-Quants.
    Explicit archives belong to the caller's saved list/archive pair; a missing
    member cannot be filled from the SQL copy or a later native head.
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
    seeds: dict[str, FilingComparisons] = {}
    duplicates: set[str] = set()
    seed_candidates = seed_reused = parsed = archive_count = archive_bytes = 0
    archive_seconds = parse_seconds = 0.0
    seed_started = time.perf_counter()
    for value in prior_observations if ready else ():
        seed_candidates += 1
        seed = successful_comparison_seed(value)
        if seed is None:
            continue
        base = observations.get(seed.source_doc_id)
        if (
            base is None
            or seed.source_doc_id not in ready
            or seed.ticker != base.ticker
            or seed.source_submit_datetime != base.source_submit_datetime
            or seed.document_type != base.document_type
        ):
            continue
        if seed.source_doc_id in seeds or seed.source_doc_id in duplicates:
            seeds.pop(seed.source_doc_id, None)
            duplicates.add(seed.source_doc_id)
        else:
            seeds[seed.source_doc_id] = seed
    seed_seconds = time.perf_counter() - seed_started
    received: set[str] = set()
    contents = _archive_contents(
        connection, doc_ids=observations.keys(), tickers=tickers, archives=archives
    )
    while True:
        read_started = time.perf_counter()
        try:
            doc_id, content = next(contents)
        except StopIteration:
            archive_seconds += time.perf_counter() - read_started
            break
        archive_seconds += time.perf_counter() - read_started
        archive_count += 1
        archive_bytes += len(content)
        received.add(doc_id)
        base = observations[doc_id]
        if doc_id not in ready:
            observations[doc_id] = base.model_copy(
                update={"source_sha256": hashlib.sha256(content).hexdigest()}
            )
            continue
        cached = seeds.pop(doc_id, None)
        # The current fixed body's integrity is always checked before reuse. A
        # warm result must not conceal a lost/tampered original or fresh blocking state.
        if cached is not None and cached.source_sha256 == hashlib.sha256(content).hexdigest():
            extracted = cached
            seed_reused += 1
        else:
            parse_started = time.perf_counter()
            extracted = extract_comparisons(
                content=content,
                ticker=base.ticker,
                doc_id=doc_id,
                submitted=ready[doc_id],
                document_type=base.document_type,
            )
            parse_seconds += time.perf_counter() - parse_started
            parsed += 1
        observations[doc_id] = extracted.model_copy(update={"parent_doc_id": base.parent_doc_id})
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
    if diagnostics is not None:
        # Iterator time includes fixed-body GET and size/hash validation (or SQL
        # reads), not network time alone. Seed time includes local L2 decoding and
        # validation. Neither measurement belongs in saved input/model identity.
        print(
            "model inputs: edinet_comparisons "
            f"selected={len(observations)} ready={len(ready)} "
            f"seed_candidates={seed_candidates} seed_reused={seed_reused} parsed={parsed} "
            f"archive_read_count={archive_count} archive_read_bytes={archive_bytes} "
            f"archive_read_validate_seconds={archive_seconds:.3f} "
            f"seed_read_validate_seconds={seed_seconds:.3f} "
            f"parse_seconds={parse_seconds:.3f}",
            file=diagnostics,
            flush=True,
        )
    return {ticker: tuple(items) for ticker, items in population.items()}


def _archive_contents(
    connection: sqlite3.Connection | duckdb.DuckDBPyConnection,
    *,
    doc_ids: Collection[str],
    tickers: Sequence[str] | None,
    archives: FixedEdinetArchives | None,
) -> Iterator[tuple[str, bytes]]:
    if not doc_ids:
        return
    if archives is not None:
        retained = archives.archive_keys
        for doc_id in doc_ids:
            if (doc_id, 1) in retained:
                # Blocked vintages still require the real bytes, not a metadata
                # hash that could hide a missing or damaged retained original.
                yield doc_id, archives.read_xbrl_zip(doc_id)
        return
    statement = "SELECT doc_id, zip_content FROM edinet_source_archives WHERE source_type = 1"
    parameters: tuple[str, ...] = ()
    if tickers is not None and len(tickers) == 1:
        # Eligibility comes from the folded observations, not this byte-read filter.
        statement += " AND doc_id IN (SELECT doc_id FROM edinet_documents WHERE sec_code = ?)"
        parameters = (tickers[0] + "0",)
    cursor = connection.execute(statement, parameters)
    while batch := cursor.fetchmany(8):
        for doc_id, content in batch:
            if doc_id in doc_ids:
                yield doc_id, bytes(content)
