"""調査用の書類参照。EDINETの提出者・対象者を買付成立や株主の受取権に置き換えない。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, computed_field

from baibai_engine.data_collection.providers.edinet.document_events import (
    canonicalize_document_events,
)
from baibai_engine.data_collection.providers.edinet.fields import (
    filing_submitted_at,
    is_usable_filing_status,
    parse_doc_id,
    parse_sec_code,
)
from baibai_engine.data_collection.providers.edinet.models import EDINETProviderError
from baibai_engine.machine_analysis.observations.disclosures.tender_offer import (
    TENDER_OFFER_REGISTRATION_CORRECTION_DOC_TYPE as _REGISTRATION_CHANGE,
)
from baibai_engine.machine_analysis.observations.disclosures.tender_offer import (
    TENDER_OFFER_REGISTRATION_DOC_TYPE as _REGISTRATION,
)
from baibai_engine.machine_analysis.observations.disclosures.tender_offer import (
    TENDER_OFFER_RESULT_CORRECTION_DOC_TYPE as _RESULT_CHANGE,
)
from baibai_engine.machine_analysis.observations.disclosures.tender_offer import (
    TENDER_OFFER_RESULT_DOC_TYPE as _RESULT,
)
from baibai_engine.machine_analysis.observations.disclosures.tender_offer import (
    TENDER_OFFER_WITHDRAWAL_DOC_TYPE as _WITHDRAWAL_NOTICE,
)
from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.primitives.time import JST

_OPINION = "290"
_LABELS = {
    _REGISTRATION: "公開買付届出書",
    _REGISTRATION_CHANGE: "訂正公開買付届出書",
    _WITHDRAWAL_NOTICE: "公開買付撤回届出書",
    _RESULT: "公開買付報告書",
    _RESULT_CHANGE: "訂正公開買付報告書",
    _OPINION: "意見表明報告書",
}
_REGISTRATION_PARENTS = (_REGISTRATION, _REGISTRATION_CHANGE)
_PARENTS = {
    _REGISTRATION_CHANGE: _REGISTRATION_PARENTS,
    _WITHDRAWAL_NOTICE: _REGISTRATION_PARENTS,
    _RESULT: _REGISTRATION_PARENTS,
    _RESULT_CHANGE: (_RESULT, _RESULT_CHANGE),
    _OPINION: _REGISTRATION_PARENTS,
}


class TenderFilingReference(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_id: str
    doc_type_code: str
    role: Literal["offeror_filing", "target_opinion"]
    filer_edinet_code: str
    subject_edinet_code: str
    registration_doc_id: str
    submitted_at: AwareDatetime
    source_date: date

    @computed_field  # type: ignore[prop-decorator]
    @property
    def document_label(self) -> str:
        return _LABELS[self.doc_type_code]


@dataclass(frozen=True, slots=True)
class _Filing:
    doc_id: str
    kind: str
    filer: str
    subject: str
    parent_id: str | None
    submitted: datetime
    day: date


def _registration_for(filing: _Filing, by_id: Mapping[str, _Filing]) -> _Filing | None:
    current = filing
    visited: set[str] = set()
    while current.kind != _REGISTRATION:
        if current.doc_id in visited:
            return None
        visited.add(current.doc_id)
        parent = by_id.get(current.parent_id) if current.parent_id else None
        if (
            parent is None
            or parent.kind not in _PARENTS[current.kind]
            or parent.submitted > current.submitted
        ):
            return None
        # Only the target's opinion reverses the two reported parties.
        parties = (
            (current.subject, current.filer)
            if current.kind == _OPINION
            else (
                current.filer,
                current.subject,
            )
        )
        if parties != (parent.filer, parent.subject):
            return None
        current = parent
    return current if current.parent_id is None else None


def resolve_tender_filing_references(
    documents: Sequence[Mapping[str, Any]], *, tickers: tuple[str, ...], known_at: datetime
) -> dict[str, tuple[TenderFilingReference, ...]]:
    """Resolve explicit families in the supplied received window, never offers without parents."""
    if known_at.utcoffset() is None:
        raise ValueError("EDINET filing reference cutoff must be timezone-aware")
    wanted = {normalize_ticker(ticker) for ticker in tickers}
    output: dict[str, list[TenderFilingReference]] = {ticker: [] for ticker in sorted(wanted)}
    through = known_at.astimezone(JST).date()
    visible = [
        row
        for row in documents
        if date.fromisoformat(row["doc_date"]) <= through
        and all(
            filing_submitted_at(row[field]) <= known_at
            for field in ("submitDateTime", "opeDateTime")
            if row.get(field)
        )
    ]
    # Fold all operations, including ones that did not repeat the target's identity.
    origins, _ = canonicalize_document_events(visible, origin_start=None)
    tickers_by_code: dict[str, set[str]] = defaultdict(set)
    codes_by_ticker: dict[str, set[str]] = defaultdict(set)
    filings: dict[str, _Filing] = {}
    for row in origins:
        if not is_usable_filing_status(
            row.get("legalStatus"), row.get("disclosureStatus"), row.get("withdrawalStatus")
        ):
            continue
        filer, subject = row.get("edinetCode"), row.get("subjectEdinetCode")
        if not isinstance(filer, str) or not filer.strip():
            continue
        if row.get("secCode"):
            try:
                ticker = parse_sec_code(row["secCode"])
            except EDINETProviderError:
                pass  # Unsupported suffixes do not identify a listed ordinary share.
            else:
                tickers_by_code[filer].add(ticker)
                codes_by_ticker[ticker].add(filer)
        kind = row.get("docTypeCode")
        if (
            (kind != _REGISTRATION and kind not in _PARENTS)
            or not isinstance(subject, str)
            or not subject.strip()
            or subject == filer
            or not row.get("submitDateTime")
        ):
            continue
        doc_id = parse_doc_id(row["docID"])
        filings[doc_id] = _Filing(
            doc_id,
            kind,
            filer,
            subject,
            parse_doc_id(row["parentDocID"]) if row.get("parentDocID") else None,
            filing_submitted_at(row["submitDateTime"]),
            date.fromisoformat(row["doc_date"]),
        )
    target_tickers = {
        code: ticker
        for ticker in wanted
        for code in codes_by_ticker[ticker]
        if len(codes_by_ticker[ticker]) == 1 and tickers_by_code[code] == {ticker}
    }
    for filing in filings.values():
        target_code = filing.filer if filing.kind == _OPINION else filing.subject
        if target_code not in target_tickers:
            continue
        registration = _registration_for(filing, filings)
        if registration is not None:
            output[target_tickers[target_code]].append(
                TenderFilingReference(
                    doc_id=filing.doc_id,
                    doc_type_code=filing.kind,
                    role="target_opinion" if filing.kind == _OPINION else "offeror_filing",
                    filer_edinet_code=filing.filer,
                    subject_edinet_code=filing.subject,
                    registration_doc_id=registration.doc_id,
                    submitted_at=filing.submitted,
                    source_date=filing.day,
                )
            )
    return {
        ticker: tuple(sorted(refs, key=lambda row: (row.submitted_at, row.doc_id)))
        for ticker, refs in output.items()
    }
