"""固定EDINET原本の年次資本注記を有限銘柄へ読む。保存・取得・株数補完はしない。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any

from baibai_engine.data_collection.providers.edinet.annual_filings import (
    annual_documents,
    usable_document,
)
from baibai_engine.data_collection.providers.edinet.archive_native import (
    ArchiveMetadata,
    FixedEdinetArchives,
)
from baibai_engine.data_collection.providers.edinet.fields import document_period, parse_sec_code
from baibai_engine.data_collection.providers.edinet.list_native import FixedEdinetLists
from baibai_engine.data_collection.providers.edinet.list_projection import project_document_list
from baibai_engine.data_collection.providers.edinet.receipts import EdinetListReceipt
from baibai_engine.machine_analysis.observations.financials.edinet_facts.models import (
    filing_identity,
)
from baibai_engine.machine_analysis.observations.financials.edinet_facts.xbrl import (
    SourceFormatError,
)
from baibai_engine.machine_analysis.observations.financials.edinet_issuer_capital import (
    CapitalNoteExtraction,
    extract_issuer_capital_notes,
)
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.primitives.time import JST


@dataclass(frozen=True, slots=True)
class IssuerCapitalReading:
    ticker: str
    document_id: str | None
    list_identity: str
    archive_identity: str
    archive: ArchiveMetadata | None
    submitted_at: datetime | None
    extraction: CapitalNoteExtraction


def _documents(lists: FixedEdinetLists, *, as_of: date, known_at: datetime) -> list[dict[str, Any]]:
    days = {
        date.fromisoformat(item["doc_date"])
        for item in lists.value()["receipts"]
        if date.fromisoformat(item["doc_date"]) <= as_of
    }
    # Do not reconstruct a historical receipt vintage by dropping later receipts
    # from a saved pair, or fetch their bodies before discovering a clock mismatch.
    if any(
        date.fromisoformat(item["doc_date"]) in days
        and datetime.fromisoformat(item["received_at_utc"]) > known_at
        for item in lists.value()["receipts"]
    ):
        raise ValueError("fixed EDINET lists contain receipts after the knowledge cutoff")
    receipts: dict[date, list[EdinetListReceipt]] = defaultdict(list)
    for item in lists.iter_receipts(days):
        receipts[item.doc_date].append(item.decoded())
    result: list[dict[str, Any]] = []
    for day in sorted(receipts):
        projection = project_document_list(receipts[day])
        if projection.error is not None:
            raise ValueError(projection.error)
        result.extend(projection.documents)
    return result


def _unavailable(reading: IssuerCapitalReading, reason: str) -> IssuerCapitalReading:
    return replace(reading, extraction=CapitalNoteExtraction((), (reason,)))


def read_issuer_capital_notes(
    lists: FixedEdinetLists,
    archives: FixedEdinetArchives,
    *,
    tickers: tuple[str, ...],
    as_of: date,
    known_at: datetime,
) -> dict[str, IssuerCapitalReading]:
    """Read selected retained annual revisions, not current heads or market SQL.

    A missing/unsupported latest correction never revives an older note. This is
    an attributed display observation; downstream pricing contracts are unchanged.
    """
    if known_at.utcoffset() is None or as_of > known_at.astimezone(JST).date():
        raise ValueError("capital note knowledge cutoff requires a valid aware timestamp")
    subjects = tuple(sorted({normalize_ticker(ticker) for ticker in tickers}))
    if not subjects:
        return {}
    documents = _documents(lists, as_of=as_of, known_at=known_at)
    eligible = []
    for document in documents:
        submitted = document.get("submitDateTime")
        if submitted:
            identity = filing_identity("", str(document["docID"]), str(submitted))
            if (
                identity.disclosed_on > as_of.isoformat()
                or datetime.fromisoformat(identity.source_submit_datetime) > known_at
            ):
                continue
        eligible.append(document)
    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for document in annual_documents(eligible):
        families[parse_sec_code(document["secCode"])].append(document)
    results = {}
    for ticker in subjects:
        reading = IssuerCapitalReading(
            ticker,
            None,
            lists.identity,
            archives.identity,
            None,
            None,
            CapitalNoteExtraction((), ()),
        )
        family = families.get(ticker, [])
        if not family:
            results[ticker] = _unavailable(reading, "capital_note_annual_family_unavailable")
            continue
        document = max(
            family, key=lambda item: (str(item.get("submitDateTime") or ""), str(item["docID"]))
        )
        doc_id = str(document["docID"])
        reading = replace(reading, document_id=doc_id)
        roots = [item for item in family if str(item.get("docTypeCode")) == "120"]
        if not usable_document(document) or len(roots) != 1:
            results[ticker] = _unavailable(reading, "capital_note_annual_revision_unavailable")
            continue
        start, end = document_period(roots[0])
        edinet_code = roots[0].get("edinetCode")
        if (
            start is None
            or end is None
            or end > as_of
            or not isinstance(edinet_code, str)
            or not edinet_code
        ):
            results[ticker] = _unavailable(reading, "capital_note_period_or_issuer_unavailable")
            continue
        revision_start, revision_end = document_period(document)
        if revision_start not in (None, start) or revision_end not in (None, end):
            results[ticker] = _unavailable(reading, "capital_note_revision_period_conflict")
            continue
        if document.get("edinetCode") not in (None, "", edinet_code):
            results[ticker] = _unavailable(reading, "capital_note_issuer_conflict")
            continue
        identity = filing_identity(ticker, doc_id, str(document.get("submitDateTime") or ""))
        reading = replace(
            reading, submitted_at=datetime.fromisoformat(identity.source_submit_datetime)
        )
        metadata = archives.metadata(doc_id, 1)
        reading = replace(reading, archive=metadata)
        if metadata is None:
            results[ticker] = _unavailable(reading, "capital_note_archive_not_retained")
            continue
        if (metadata.received_at_utc is not None and metadata.received_at_utc > known_at) or (
            metadata.received_at_utc is None
            and metadata.stored_on >= known_at.astimezone(UTC).date()
        ):
            results[ticker] = _unavailable(reading, "capital_note_archive_receipt_unconfirmed")
            continue
        edits = [
            str(item.get("doc_date") or "")
            for item in eligible
            if str(item.get("docID")) == doc_id and str(item.get("docInfoEditStatus")) == "1"
        ]
        archive_day = (
            metadata.received_at_utc.astimezone(JST).date()
            if metadata.received_at_utc is not None
            else metadata.stored_on
        )
        if (edits and max(edits) >= archive_day.isoformat()) or (
            str(document.get("docInfoEditStatus")) == "2" and not edits
        ):
            results[ticker] = _unavailable(reading, "capital_note_archive_revision_unconfirmed")
            continue
        # Missing or changed retained bytes are a storage failure, not a missing
        # disclosure. Only unsupported source structure becomes a per-issuer reason.
        content = archives.read_xbrl_zip(doc_id)
        try:
            extracted = extract_issuer_capital_notes(
                content, edinet_code=edinet_code, period_start=start, period_end=end
            )
        except SourceFormatError as error:
            extracted = CapitalNoteExtraction((), (str(error),))
        results[ticker] = replace(reading, extraction=extracted)
    return results
