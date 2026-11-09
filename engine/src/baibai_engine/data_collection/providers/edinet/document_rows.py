"""Native document field/row contract shared by fixed-list planning and the SQL projection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from baibai_engine.data_collection.providers.edinet.fields import coalesce_field
from baibai_engine.data_collection.providers.edinet.models import EDINETProviderError
from baibai_engine.primitives.missing import is_missing_scalar

_DOCUMENT_COLUMNS = (
    "doc_date",
    "seqNumber",
    "docID",
    "secCode",
    "docTypeCode",
    "csvFlag",
    "xbrlFlag",
    "legalStatus",
    "disclosureStatus",
    "withdrawalStatus",
    "docInfoEditStatus",
    "parentDocID",
    "opeDateTime",
    "submitDateTime",
    "docDescription",
    "periodStart",
    "periodEnd",
    "edinetCode",
    "issuerEdinetCode",
    "subjectEdinetCode",
)


def document_text(value: Any) -> str | None:
    """Preserve the retained document-row scalar interpretation."""
    if is_missing_scalar(value):
        return None
    return str(value).strip()


def _document_date(value: Any) -> str | None:
    if is_missing_scalar(value):
        return None
    return str(value)[:10]


def projected_documents(
    doc_date: str, records: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    return [
        dict(zip(_DOCUMENT_COLUMNS, row, strict=True)) for row in document_rows(doc_date, records)
    ]


def document_inputs(
    projected_documents: Sequence[Mapping[str, Any]],
    native_rows: Mapping[tuple[str, int, str], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve native keys only after the caller's date/document scope is selected.

    Ordinary filings retain normalized fields observed across source receipts.
    Operation rows use only keys supplied by the selected native response, so
    explicit nulls clear metadata while omissions do not.
    """
    inputs = []
    for projected in projected_documents:
        day = str(projected["doc_date"])
        identity = (int(projected["seqNumber"]), str(projected["docID"]))
        source = native_rows.get((day, *identity))
        if source is None:
            raise EDINETProviderError(
                f"EDINET stored document has no matching native row: {day}/{identity}"
            )
        inputs.append(
            {**source, "doc_date": day} if is_document_operation(source) else dict(projected)
        )
    return inputs


def document_rows(
    doc_date: str,
    records: Iterable[Mapping[str, Any]],
) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    sequence_numbers: set[int] = set()
    for record in records:
        doc_id = document_text(coalesce_field(record, "docID", "doc_id"))
        if doc_id is None:
            raise ValueError("EDINET document row is missing docID")
        raw_sequence = coalesce_field(record, "seqNumber", "sequence_number")
        if raw_sequence in (None, ""):
            raise ValueError("EDINET document row is missing seqNumber")
        if isinstance(raw_sequence, bool):
            raise ValueError(f"invalid EDINET seqNumber: {raw_sequence!r}")
        if isinstance(raw_sequence, int):
            sequence_number = raw_sequence
        elif isinstance(raw_sequence, str) and raw_sequence.isdecimal():
            sequence_number = int(raw_sequence)
        else:
            raise ValueError(f"invalid EDINET seqNumber: {raw_sequence!r}")
        if sequence_number <= 0:
            raise ValueError(f"invalid EDINET seqNumber: {raw_sequence!r}")
        if sequence_number in sequence_numbers:
            raise ValueError(f"duplicate EDINET seqNumber: {sequence_number}")
        sequence_numbers.add(sequence_number)
        rows.append(
            (
                doc_date,
                sequence_number,
                doc_id,
                document_text(coalesce_field(record, "secCode", "sec_code")),
                document_text(coalesce_field(record, "docTypeCode", "doc_type_code")),
                document_text(coalesce_field(record, "csvFlag", "csv_flag")),
                document_text(coalesce_field(record, "xbrlFlag", "xbrl_flag")),
                document_text(coalesce_field(record, "legalStatus", "legal_status")),
                document_text(coalesce_field(record, "disclosureStatus", "disclosure_status")),
                document_text(coalesce_field(record, "withdrawalStatus", "withdrawal_status")),
                document_text(coalesce_field(record, "docInfoEditStatus", "doc_info_edit_status")),
                document_text(coalesce_field(record, "parentDocID", "parent_doc_id")),
                document_text(coalesce_field(record, "opeDateTime", "operation_datetime")),
                document_text(coalesce_field(record, "submitDateTime", "submit_datetime")),
                document_text(coalesce_field(record, "docDescription", "doc_description")),
                _document_date(coalesce_field(record, "periodStart", "period_start")),
                _document_date(coalesce_field(record, "periodEnd", "period_end")),
                document_text(coalesce_field(record, "edinetCode", "edinet_code")),
                document_text(coalesce_field(record, "issuerEdinetCode", "issuer_edinet_code")),
                document_text(coalesce_field(record, "subjectEdinetCode", "subject_edinet_code")),
            )
        )
    return rows


def is_document_operation(document: Mapping[str, Any]) -> bool:
    return (
        str(document.get("docInfoEditStatus")) == "1"
        or str(document.get("withdrawalStatus")) == "1"
        or str(document.get("disclosureStatus")) in {"1", "3"}
    )


def native_document_index(
    payload: object, *, doc_date: str
) -> dict[tuple[int, str], dict[str, Any]]:
    results = payload.get("results") if isinstance(payload, Mapping) else None
    if not isinstance(results, list):
        raise EDINETProviderError(f"EDINET saved response has no results list: {doc_date}")
    documents: dict[tuple[int, str], dict[str, Any]] = {}
    for source in results:
        if not isinstance(source, Mapping):
            raise EDINETProviderError(f"EDINET saved response has an invalid row: {doc_date}")
        sequence = source.get("seqNumber")
        doc_id = source.get("docID")
        if (
            isinstance(sequence, bool)
            or not isinstance(sequence, (int, str))
            or not str(sequence).isdecimal()
            or int(sequence) <= 0
            or not isinstance(doc_id, str)
            or not doc_id
        ):
            raise EDINETProviderError(f"EDINET saved row has an invalid identity: {doc_date}")
        identity = (int(sequence), doc_id)
        if identity in documents:
            raise EDINETProviderError(f"EDINET saved response repeats a row: {doc_date}/{identity}")
        documents[identity] = dict(source)
    return documents
