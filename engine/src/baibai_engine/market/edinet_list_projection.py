"""Reconstruct one EDINET document list using retained receipts, never prior L2 rows."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import groupby
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from baibai_engine.collection.edinet.receipts import EdinetListReceipt
from baibai_engine.market.edinet_document_events import _validate_document_statuses, parse_doc_id
from baibai_engine.market.edinet_models import EDINETProviderError

_RETAINED_NATIVE_FIELDS = (
    "secCode",
    "docTypeCode",
    "parentDocID",
    "submitDateTime",
    "docDescription",
    "edinetCode",
    "issuerEdinetCode",
    "subjectEdinetCode",
)
_JST = ZoneInfo("Asia/Tokyo")


@dataclass(frozen=True, slots=True)
class DocumentListProjection:
    doc_date: date
    latest_received_at: datetime
    selected_receipt: EdinetListReceipt | None
    native_payload: dict[str, Any] | None
    documents: tuple[dict[str, Any], ...]
    error: str | None


def project_document_list(receipts: Sequence[EdinetListReceipt]) -> DocumentListProjection:
    if not receipts:
        raise ValueError("EDINET projection requires at least one receipt")
    doc_date = receipts[0].doc_date
    if any(receipt.doc_date != doc_date for receipt in receipts):
        raise ValueError("EDINET projection requires receipts for one requested day")
    if any(receipt.received_at.utcoffset() != timedelta(0) for receipt in receipts):
        raise ValueError("EDINET projection receipt times must be UTC")
    ordered = sorted(receipts, key=lambda receipt: receipt.received_at)
    selected: EdinetListReceipt | None = None
    payload: dict[str, Any] | None = None
    documents: tuple[dict[str, Any], ...] = ()
    observed: dict[tuple[int, str], dict[str, Any]] = {}
    error: str | None = None
    for _stamp, same_time in groupby(ordered, key=lambda receipt: receipt.received_at):
        group = list(same_time)
        if len({receipt.body for receipt in group}) != 1:
            error = "EDINET receipt order is ambiguous at the same received time"
            break
        # Identical bytes have identical source meaning. This only chooses a stable
        # provenance reference; UUID order is never treated as acquisition chronology.
        receipt = min(group, key=lambda receipt: receipt.receipt_id)
        try:
            candidate_payload, candidate_documents, keys = _decode_receipt(receipt)
            next_observed: dict[tuple[int, str], dict[str, Any]] = {}
            enriched: list[dict[str, Any]] = []
            for key, record in zip(keys, candidate_documents, strict=True):
                projected = dict(record)
                previous = observed.get(key)
                if previous is not None and _is_unavailable_origin(record):
                    for field in _RETAINED_NATIVE_FIELDS:
                        if projected.get(field) in (None, "") and field in previous:
                            projected[field] = previous[field]
                projected["doc_date"] = doc_date.isoformat()
                next_observed[key] = projected
                enriched.append(projected)
        except (ValueError, UnicodeError, EDINETProviderError) as exc:
            error = f"EDINET list projection failed: {type(exc).__name__}: {exc}"
            continue
        selected = receipt
        payload = candidate_payload
        documents = tuple(enriched)
        observed = next_observed
        error = None
    return DocumentListProjection(
        doc_date=doc_date,
        latest_received_at=ordered[-1].received_at,
        selected_receipt=selected,
        native_payload=payload,
        documents=documents,
        error=error,
    )


def document_list_rechecked_asof(
    projection: DocumentListProjection, current_day_receipts: Sequence[EdinetListReceipt]
) -> date | None:
    """Prove a later day's official list was received before the selected historical list."""
    selected = projection.selected_receipt
    if projection.error is not None or selected is None:
        return None
    recheck_day = selected.received_at.astimezone(_JST).date()
    if projection.doc_date >= recheck_day:
        return None
    for receipt in current_day_receipts:
        if (
            receipt.doc_date != recheck_day
            or receipt.received_at.utcoffset() != timedelta(0)
            or receipt.received_at.astimezone(_JST).date() != recheck_day
            or receipt.received_at >= selected.received_at
        ):
            continue
        try:
            _decode_receipt(receipt)
        except (ValueError, UnicodeError, EDINETProviderError):
            continue
        return recheck_day
    return None


def _reject_nonfinite_constant(value: str) -> None:
    raise ValueError(f"EDINET response JSON contains nonfinite constant: {value}")


def _parse_finite_float(value: str) -> float:
    parsed = float(value)
    if not isfinite(parsed):
        raise ValueError(f"EDINET response JSON contains nonfinite float: {value}")
    return parsed


def _decode_receipt(
    receipt: EdinetListReceipt,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[tuple[int, str]]]:
    payload = json.loads(
        receipt.body,
        parse_constant=_reject_nonfinite_constant,
        parse_float=_parse_finite_float,
    )
    if not isinstance(payload, dict):
        raise ValueError("EDINET response JSON must be an object")
    results = payload.get("results")
    if not isinstance(results, list) or any(not isinstance(item, dict) for item in results):
        raise ValueError("EDINET results must be a list of document objects")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("resultset"), dict):
        raise ValueError("EDINET response is missing resultset metadata")
    count = metadata["resultset"].get("count")
    try:
        valid_count = int(str(count))
    except ValueError:
        raise ValueError("EDINET resultset count is invalid") from None
    if valid_count != len(results):
        raise ValueError("EDINET resultset count does not match the result rows")
    if "status" in metadata and str(metadata["status"]) != "200":
        raise ValueError("EDINET metadata status is not successful")
    if "parameter" in metadata:
        parameter = metadata["parameter"]
        if not isinstance(parameter, dict) or parameter.get("date") != receipt.doc_date.isoformat():
            raise ValueError("EDINET response date does not match the requested day")
    records = [dict(item) for item in results]
    for record in records:
        _validate_document_statuses(record)
    keys: list[tuple[int, str]] = []
    sequences: set[int] = set()
    for record in records:
        raw_sequence = record.get("seqNumber")
        if isinstance(raw_sequence, bool):
            raise ValueError("EDINET seqNumber must be a positive integer")
        if isinstance(raw_sequence, int):
            sequence = raw_sequence
        elif isinstance(raw_sequence, str) and raw_sequence.isascii() and raw_sequence.isdecimal():
            sequence = int(raw_sequence)
        else:
            raise ValueError("EDINET seqNumber must be a positive integer")
        if sequence <= 0 or sequence in sequences:
            raise ValueError("EDINET seqNumber must be positive and unique within the list")
        sequences.add(sequence)
        keys.append((sequence, parse_doc_id(record.get("docID"))))
    return payload, records, keys


def _is_unavailable_origin(record: dict[str, Any]) -> bool:
    edit = str(record.get("docInfoEditStatus"))
    withdrawal = str(record.get("withdrawalStatus"))
    disclosure = str(record.get("disclosureStatus"))
    if edit == "1" or withdrawal == "1" or disclosure in {"1", "3"}:
        return False
    return str(record.get("legalStatus")) == "0" or withdrawal == "2"
