"""Direct, attributed share-count observations in retained EDINET holder filings.

A large holder's report is not an issuer attestation or proof of a change date.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from xml.etree.ElementTree import Element  # nosec B405 -- type annotation only

from baibai_engine.machine_analysis.observations.financials.edinet_facts.xbrl import (
    XBRLI,
    Context,
    expand_qname,
    local_name,
    read_instance,
)
from baibai_engine.primitives.stocks import SecurityCodeError, parse_security_code_parts
from baibai_engine.primitives.time import JST

if TYPE_CHECKING:
    import duckdb

_NAMESPACE = re.compile(
    r"^\{http://disclosure\.edinet-fsa\.go\.jp/taxonomy/jplvh/\d{4}-\d{2}-\d{2}/jplvh_cor\}"
)


@dataclass(frozen=True, slots=True)
class HolderReportedCapital:
    ticker: str
    reference_date: date
    submitted_at: datetime
    document_id: str
    total_stocks: int
    original_sha256: str
    source_kind: str = "third_party_holder_report_not_issuer_attestation"


def extract_holder_capital(
    content: bytes, *, document_id: str, submitted_at: datetime, known_at: datetime
) -> HolderReportedCapital | None:
    """Read an unambiguous standard fact without treating it as a corporate action."""
    if submitted_at.utcoffset() is None or known_at.utcoffset() is None:
        raise ValueError("disclosure and knowledge timestamps require timezones")
    if submitted_at > known_at:
        return None
    instance = read_instance(content, include_labels=False)
    share_units = set()
    for unit in instance.root.findall(f"{{{XBRLI}}}unit"):
        measures = unit.findall(f"{{{XBRLI}}}measure")
        if len(measures) != 1 or len(unit) != 1 or not measures[0].text:
            continue
        if expand_qname(measures[0].text, instance.namespaces) == f"{{{XBRLI}}}shares":
            share_units.add(unit.get("id", ""))
    facts: dict[str, list[tuple[Element, Context]]] = {}
    for element in instance.root:
        if not _NAMESPACE.match(element.tag):
            continue
        name = local_name(element.tag)
        if name not in {"SecurityCodeOfIssuer", "TotalNumberOfOutstandingStocksEtc"}:
            continue
        context = instance.contexts.get(element.get("contextRef", ""))
        if context is None or context.dimensions or context.period_start is not None:
            continue
        facts.setdefault(name, []).append((element, context))
    codes = facts.get("SecurityCodeOfIssuer", [])
    totals = facts.get("TotalNumberOfOutstandingStocksEtc", [])
    if len(codes) != 1 or len(totals) != 1:
        return None
    code, code_context = codes[0]
    total, total_context = totals[0]
    try:
        ticker, common_code = parse_security_code_parts(code.text)
    except SecurityCodeError:
        return None
    raw = (total.text or "").strip()
    if not common_code or code_context.period_end != total_context.period_end:
        return None
    reference = date.fromisoformat(total_context.period_end)
    if (
        reference > submitted_at.astimezone(JST).date()
        or not raw.isascii()
        or not raw.isdigit()
        or int(raw) <= 0
    ):
        return None
    if total.get("unitRef") not in share_units or total.get("decimals") not in {"0", "INF"}:
        return None
    return HolderReportedCapital(
        ticker, reference, submitted_at, document_id, int(raw), hashlib.sha256(content).hexdigest()
    )


def read_holder_capital(
    connection: sqlite3.Connection | duckdb.DuckDBPyConnection,
    *,
    as_of: date,
    known_at: datetime,
    tickers: tuple[str, ...],
    snapshot_sealed_at: datetime | None = None,
) -> tuple[dict[str, tuple[HolderReportedCapital, ...]], str]:
    """Use only already-retained originals; absence is not proof of no change.

    Archive retention currently records a calendar day, not an intraday receipt
    timestamp. Treat the result as research evidence, not a point-in-time
    backtest or an issuer-certified capital state.
    """
    from baibai_engine.machine_analysis.observations.disclosures.edinet_document_events import (
        _canonicalize_document_events,
    )
    from baibai_engine.machine_analysis.observations.disclosures.edinet_document_store import (
        read_observed_documents,
    )
    from baibai_engine.machine_analysis.observations.financials.edinet_decode import (
        is_usable_filing_status,
    )
    from baibai_engine.machine_analysis.observations.financials.edinet_facts.xbrl import (
        SourceFormatError,
    )

    if known_at.utcoffset() is None or (
        snapshot_sealed_at is not None and snapshot_sealed_at.utcoffset() is None
    ):
        raise ValueError("knowledge cutoff and snapshot clock require timezones")
    selected = set(tickers)
    documents = read_observed_documents(connection, through=as_of, holder_only=True)
    # A retained list may have been acquired after the requested cutoff. Keep
    # only days with a verified native receipt known by both clocks.
    valid_days = set()
    for day, received in connection.execute(
        "SELECT doc_date,selected_received_at_utc FROM edinet_document_lists "
        "WHERE doc_date<=? AND selected_received_at_utc IS NOT NULL",
        (as_of.isoformat(),),
    ).fetchall():
        try:
            receipt = datetime.fromisoformat(str(received))
        except ValueError:
            continue
        if (
            receipt.utcoffset() is not None
            and receipt <= known_at
            and receipt.astimezone(JST).date() <= as_of
        ):
            valid_days.add(str(day))
    holder_days = {
        str(doc.get("doc_date"))
        for doc in documents
        if str(doc.get("docTypeCode")) in {"350", "360"}
    }
    missing_list_days = len(holder_days - valid_days)
    documents = [doc for doc in documents if str(doc.get("doc_date")) in valid_days]
    # A cached archive is immutable by identity. If the same EDINET document
    # was edited on or after its coarse receipt day, we cannot prove that the
    # retained bytes reflect the edit; keep them but do not present them as current.
    edited_on: dict[str, str] = {}
    for document in documents:
        if str(document.get("docInfoEditStatus")) == "1" and document.get("docID"):
            key = str(document["docID"])
            edited_on[key] = max(edited_on.get(key, ""), str(document.get("doc_date") or ""))
    origins, quarantined = _canonicalize_document_events(documents, origin_start=None)
    observations: dict[str, list[HolderReportedCapital]] = {}
    # Read the small identity inventory once. Query original bytes only for a
    # retained eligible filing, not once for every missing historical filing.
    archive_days = {
        str(doc_id): stored_on
        for doc_id, stored_on in connection.execute(
            "SELECT doc_id,stored_on FROM edinet_source_archives WHERE source_type=1"
        ).fetchall()
    }
    considered = missing = unsupported = 0
    for document in origins:
        if str(document.get("docTypeCode")) not in {"350", "360"}:
            continue
        if not is_usable_filing_status(
            document.get("legalStatus"),
            document.get("disclosureStatus"),
            document.get("withdrawalStatus"),
        ):
            continue
        if str(document.get("xbrlFlag")) != "1":
            continue
        considered += 1
        doc_id = str(document.get("docID") or "")
        submitted_raw = document.get("submitDateTime")
        if not doc_id or not isinstance(submitted_raw, str):
            unsupported += 1
            continue
        try:
            submitted = datetime.fromisoformat(submitted_raw)
            submitted = (
                submitted.replace(tzinfo=JST)
                if submitted.tzinfo is None
                else submitted.astimezone(JST)
            )
        except ValueError:
            unsupported += 1
            continue
        if submitted > known_at:
            continue
        stored_value = archive_days.get(doc_id)
        if stored_value is None:
            missing += 1
            continue
        try:
            stored_on = date.fromisoformat(str(stored_value))
        except ValueError:
            unsupported += 1
            continue
        if edited_on.get(doc_id, "") >= stored_on.isoformat():
            unsupported += 1
            continue
        # The UTC receipt day alone cannot locate a same-day receipt before a
        # cutoff. A snapshot sealed by that cutoff is an independent upper bound
        # on when the retained bytes were already present.
        proven_by_snapshot = (
            snapshot_sealed_at is not None
            and snapshot_sealed_at <= known_at
            and snapshot_sealed_at.astimezone(JST).date() <= as_of
            and stored_on <= snapshot_sealed_at.astimezone(UTC).date()
        )
        if not proven_by_snapshot and stored_on >= min(as_of, known_at.astimezone(UTC).date()):
            missing += 1
            continue
        row = connection.execute(
            "SELECT zip_content FROM edinet_source_archives WHERE doc_id=? AND source_type=1",
            (doc_id,),
        ).fetchone()
        if row is None:
            unsupported += 1
            continue
        try:
            fact = extract_holder_capital(
                bytes(row[0]), document_id=doc_id, submitted_at=submitted, known_at=known_at
            )
        except SourceFormatError:
            unsupported += 1
            continue
        if fact is None:
            unsupported += 1
            continue
        if fact.ticker in selected and fact.reference_date <= as_of:
            observations.setdefault(fact.ticker, []).append(fact)
    logging.getLogger(__name__).info(
        "holder-capital originals considered=%s missing=%s unsupported=%s",
        considered,
        missing,
        unsupported,
    )
    coverage = (
        "partial"
        if missing or unsupported or quarantined or missing_list_days
        else "not_observed"
        if not considered
        else "all_retained_considered"
    )
    return (
        {
            ticker: tuple(sorted(items, key=lambda item: (item.submitted_at, item.document_id)))
            for ticker, items in observations.items()
        },
        coverage,
    )
