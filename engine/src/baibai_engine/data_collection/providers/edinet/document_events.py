"""Pure EDINET operation folding shared by source selection and stored readers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass

from baibai_engine.data_collection.providers.edinet.fields import (
    DocumentOperation,
    coalesce_field,
    parse_sec_code,
    safe_error_value,
    text_or_none,
    validated_document_operation,
)
from baibai_engine.data_collection.providers.edinet.fields import (
    parse_doc_id as _parse_doc_id,
)
from baibai_engine.data_collection.providers.edinet.models import EDINETProviderError

_MODEL_CONFIG = ConfigDict(strict=True, arbitrary_types_allowed=False)


def parse_doc_id(value: object) -> str:
    try:
        return _parse_doc_id(value)
    except ValueError as error:
        raise EDINETProviderError(str(error)) from error


@dataclass(frozen=True, slots=True, config=_MODEL_CONFIG)
class QuarantinedDocumentEvent:
    """An operation row whose origin filing cannot be resolved from retained input."""

    doc_id: str
    target_doc_id: str | None
    event_type: str
    document_type: str | None = None
    ticker: str | None = None


def canonicalize_document_events(
    documents: Sequence[Mapping[str, Any]],
    *,
    origin_start: date | None,
) -> tuple[list[dict[str, Any]], tuple[QuarantinedDocumentEvent, ...]]:
    """Fold EDINET operation rows onto origin filings before ticker selection.

    The origin window limits returned filings, not the retained evidence used to
    resolve operations and parent withdrawals. Only an actually missing origin
    produces a quarantined event.
    """
    normalized = [dict(document) for document in documents]
    normalized.sort(key=_document_event_sort_key)
    origins: dict[str, dict[str, Any]] = {}
    events: list[tuple[str, dict[str, Any], DocumentOperation]] = []
    for document in normalized:
        try:
            operation = validated_document_operation(document)
        except ValueError as error:
            raise EDINETProviderError(str(error)) from error
        doc_id_raw = coalesce_field(document, "docID", "doc_id")
        if doc_id_raw is None:
            continue
        doc_id = parse_doc_id(doc_id_raw)
        if operation is not None:
            events.append((doc_id, document, operation))
            continue
        origins[doc_id] = document

    children: dict[str, set[str]] = {}
    for doc_id, origin in origins.items():
        parent = text_or_none(coalesce_field(origin, "parentDocID", "parent_doc_id"))
        if parent:
            children.setdefault(parent, set()).add(doc_id)
    _validate_parent_graph(origins)

    quarantined: set[QuarantinedDocumentEvent] = set()
    withdrawn_by_event: set[str] = set()
    parents_changed = False
    for doc_id, event, operation in events:
        if operation.withdrawal == "1":
            parent = text_or_none(coalesce_field(event, "parentDocID", "parent_doc_id"))
            if parent is None or parent not in origins:
                quarantined.add(
                    _quarantined_document_event(
                        event,
                        doc_id=doc_id,
                        target_doc_id=parent,
                        event_type="withdrawal",
                    )
                )
                continue
            withdrawn_by_event.update(
                _tombstone_document_tree(parent, origins=origins, children=children)
            )
            continue
        current = origins.get(doc_id)
        if current is None:
            event_type = "edit" if operation.edit == "1" else "disclosure"
            quarantined.add(
                _quarantined_document_event(
                    event,
                    doc_id=doc_id,
                    target_doc_id=doc_id,
                    event_type=event_type,
                )
            )
            continue
        if doc_id in withdrawn_by_event:
            raise EDINETProviderError(f"EDINET event follows withdrawal: {doc_id}")
        # Refreshed origin flags describe current state, not the time of an older
        # immutable operation row. Apply its metadata without reviving a tombstone.
        was_withdrawn = (
            text_or_none(coalesce_field(current, "withdrawalStatus", "withdrawal_status")) == "2"
        )
        origin_date = current.get("doc_date")
        old_parent = text_or_none(coalesce_field(current, "parentDocID", "parent_doc_id"))
        # Supplied nulls replace metadata; omitted keys leave it alone. The stored
        # reader preserves native operation keys and omits unknown SQL placeholders
        # when an import has no original response to establish key presence.
        current.update(event)
        new_parent = text_or_none(coalesce_field(current, "parentDocID", "parent_doc_id"))
        if new_parent != old_parent:
            parents_changed = True
            if old_parent is not None:
                children[old_parent].discard(doc_id)
            if new_parent is not None:
                children.setdefault(new_parent, set()).add(doc_id)
        if origin_date is not None:
            current["doc_date"] = origin_date
        if was_withdrawn:
            current["withdrawalStatus"] = "2"
            current["legalStatus"] = "0"
        if operation.edit == "1":
            current["docInfoEditStatus"] = "2"
        if operation.disclosure == "1":
            current["disclosureStatus"] = "2"
        elif operation.disclosure == "3":
            current["disclosureStatus"] = "0"
    if parents_changed:
        _validate_parent_graph(origins)
    selected_origins: list[dict[str, Any]] = []
    for origin in origins.values():
        origin_date = text_or_none(coalesce_field(origin, "doc_date"))
        if origin_start is not None and origin_date is not None:
            try:
                if date.fromisoformat(origin_date) < origin_start:
                    continue
            except ValueError as exc:
                raise EDINETProviderError(
                    f"invalid EDINET origin date: {safe_error_value(origin_date)}"
                ) from exc
        selected_origins.append(origin)
    return selected_origins, tuple(
        sorted(
            quarantined,
            key=lambda item: (
                item.doc_id,
                item.event_type,
                item.target_doc_id or "",
                item.ticker or "",
            ),
        )
    )


def _quarantined_document_event(
    event: Mapping[str, Any],
    *,
    doc_id: str,
    target_doc_id: str | None,
    event_type: str,
) -> QuarantinedDocumentEvent:
    raw_sec_code = coalesce_field(event, "secCode", "sec_code")
    ticker: str | None = None
    if text_or_none(raw_sec_code) is not None:
        try:
            ticker = parse_sec_code(raw_sec_code)
        except EDINETProviderError:
            # The event is already quarantined. An unusable security code reduces
            # the isolation scope to the event itself rather than restoring a
            # batch-wide failure for metadata that cannot identify a ticker.
            ticker = None
    document_type = text_or_none(coalesce_field(event, "docTypeCode", "doc_type_code"))
    if document_type is not None and (
        len(document_type) != 3 or not document_type.isascii() or not document_type.isdigit()
    ):
        document_type = None
    return QuarantinedDocumentEvent(
        doc_id=doc_id,
        target_doc_id=target_doc_id,
        event_type=event_type,
        document_type=document_type,
        ticker=ticker,
    )


def _validate_parent_graph(origins: Mapping[str, Mapping[str, Any]]) -> None:
    """Reject cycles even when no withdrawal event traverses the relation."""
    complete: set[str] = set()
    for start in origins:
        path: set[str] = set()
        current = start
        while current in origins and current not in complete:
            if current in path:
                raise EDINETProviderError(f"EDINET parent relation cycle detected: {current}")
            path.add(current)
            parent = text_or_none(coalesce_field(origins[current], "parentDocID", "parent_doc_id"))
            if parent is None:
                break
            current = parent
        complete.update(path)


def _tombstone_document_tree(
    root: str,
    *,
    origins: dict[str, dict[str, Any]],
    children: Mapping[str, set[str]],
) -> set[str]:
    pending = [root]
    seen: set[str] = set()
    while pending:
        doc_id = pending.pop()
        if doc_id in seen:
            raise EDINETProviderError(f"EDINET parent relation cycle detected: {doc_id}")
        seen.add(doc_id)
        origin = origins.get(doc_id)
        if origin is not None:
            origin["withdrawalStatus"] = "2"
            origin["legalStatus"] = "0"
        pending.extend(children.get(doc_id, ()))
    return seen


def _document_event_sort_key(document: Mapping[str, Any]) -> tuple[str, str, int]:
    doc_date = text_or_none(coalesce_field(document, "doc_date")) or ""
    operation = text_or_none(coalesce_field(document, "opeDateTime", "operation_datetime")) or ""
    raw_sequence = coalesce_field(document, "seqNumber", "sequence_number")
    try:
        sequence = int(raw_sequence or 0)
    except (TypeError, ValueError):
        sequence = 0
    return doc_date, operation, sequence
