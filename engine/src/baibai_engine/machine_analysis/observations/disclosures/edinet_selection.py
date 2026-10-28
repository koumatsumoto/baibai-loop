"""Pure EDINET filing candidate selection from document observations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from pydantic import ConfigDict, field_validator
from pydantic.dataclasses import dataclass

from baibai_engine.primitives.stocks import normalize_ticker
from baibai_engine.market.edinet_decode import (
    coalesce_field,
    document_period,
    is_usable_filing_status,
    parse_sec_code,
    text_or_none,
)
from baibai_engine.machine_analysis.observations.disclosures.edinet_document_events import (
    QuarantinedDocumentEvent,
    _canonicalize_document_events,
    parse_doc_id,
)

_MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False)


TARGET_DOC_TYPE_CODES = frozenset({"120", "130", "140", "150", "160", "170"})


CORRECTION_DOC_TYPE_CODES = frozenset({"130", "150", "170"})


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class EdinetDocumentCandidate:
    ticker: str
    doc_id: str
    doc_type_code: str
    submit_datetime: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    source_document_revision: str | None = None

    @field_validator("ticker", mode="before")
    @classmethod
    def _normalize_ticker_field(cls, value: str) -> str:
        return normalize_ticker(value)


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class DocumentSelection:
    """Selected filings plus operation rows quarantined without an origin filing.

    EDINET lists an edit or withdrawal on the day it happens, so an operation on
    a filing older than the lookback window arrives with its origin absent. Such
    an event cannot safely mutate a candidate. The structured quarantine keeps
    its identity and any usable ticker visible so extraction can fail-close that
    ticker without letting one external inconsistency stop the whole batch.
    """

    candidates: dict[str, EdinetDocumentCandidate]
    quarantined_events: tuple[QuarantinedDocumentEvent, ...] = ()


def select_document_candidates(
    documents: Sequence[Mapping[str, Any]],
    *,
    origin_start: date | None = None,
) -> DocumentSelection:
    """Select the latest usable EDINET CSV-capable filing per ticker."""
    candidates: dict[str, EdinetDocumentCandidate] = {}
    folded, quarantined = _canonicalize_document_events(documents, origin_start=origin_start)
    for document in folded:
        raw_doc_id = coalesce_field(document, "docID", "doc_id")
        raw_type = text_or_none(coalesce_field(document, "docTypeCode", "doc_type_code"))
        if raw_doc_id is None or raw_type not in TARGET_DOC_TYPE_CODES:
            continue
        if text_or_none(coalesce_field(document, "csvFlag", "csv_flag")) != "1":
            continue
        if text_or_none(coalesce_field(document, "xbrlFlag", "xbrl_flag")) != "1":
            continue
        if _is_unusable_status(document):
            continue
        doc_id = parse_doc_id(raw_doc_id)
        raw_sec_code = coalesce_field(document, "secCode", "sec_code")
        if text_or_none(raw_sec_code) is None:
            continue
        ticker = parse_sec_code(raw_sec_code)
        period_start, period_end = document_period(document)
        candidate = EdinetDocumentCandidate(
            ticker=ticker,
            doc_id=doc_id,
            doc_type_code=raw_type,
            submit_datetime=text_or_none(coalesce_field(document, "submitDateTime")),
            period_start=period_start,
            period_end=period_end,
            source_document_revision=_source_document_revision(document),
        )
        current = candidates.get(ticker)
        if current is None or _document_sort_key(candidate) > _document_sort_key(current):
            candidates[ticker] = candidate
    return DocumentSelection(candidates=candidates, quarantined_events=quarantined)


def _source_document_revision(document: Mapping[str, Any]) -> str:
    """Identify the canonical EDINET document state that drives extraction."""
    field_aliases = {
        "doc_id": ("docID", "doc_id"),
        "security_code": ("secCode", "sec_code"),
        "document_type_code": ("docTypeCode", "doc_type_code"),
        "csv_flag": ("csvFlag", "csv_flag"),
        "xbrl_flag": ("xbrlFlag", "xbrl_flag"),
        "legal_status": ("legalStatus", "legal_status"),
        "disclosure_status": ("disclosureStatus", "disclosure_status"),
        "withdrawal_status": ("withdrawalStatus", "withdrawal_status"),
        "edit_status": ("docInfoEditStatus", "doc_info_edit_status"),
        "parent_doc_id": ("parentDocID", "parent_doc_id"),
        "operation_datetime": ("opeDateTime", "operation_datetime"),
        "submit_datetime": ("submitDateTime", "submit_datetime"),
        "description": ("docDescription", "description"),
        "period_start": ("periodStart", "period_start"),
        "period_end": ("periodEnd", "period_end"),
        "sequence_number": ("seqNumber", "sequence_number"),
    }
    payload = {
        name: next(
            (
                str(document[alias]).strip()
                for alias in aliases
                if document.get(alias) not in (None, "")
            ),
            None,
        )
        for name, aliases in field_aliases.items()
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_unusable_status(document: Mapping[str, Any]) -> bool:
    return not is_usable_filing_status(
        coalesce_field(document, "legalStatus", "legal_status"),
        coalesce_field(document, "disclosureStatus", "disclosure_status"),
        coalesce_field(document, "withdrawalStatus", "withdrawal_status"),
    )


def _document_sort_key(
    candidate: EdinetDocumentCandidate,
) -> tuple[int, date, int, date, int, str, int, str]:
    correction = 1 if candidate.doc_type_code in CORRECTION_DOC_TYPE_CODES else 0
    return (
        1 if candidate.period_end is not None else 0,
        candidate.period_end or date.min,
        1 if candidate.period_start is not None else 0,
        candidate.period_start or date.min,
        # Interim metadata can describe the whole fiscal year. For a known
        # window, a late interim correction must not replace annual statements.
        # Missing or invalid metadata cannot establish that window.
        int(
            candidate.doc_type_code in {"120", "130"}
            and candidate.period_start is not None
            and candidate.period_end is not None
            and candidate.period_start < candidate.period_end
        ),
        candidate.submit_datetime or "",
        correction,
        candidate.doc_id,
    )
