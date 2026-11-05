"""Native EDINET identity and status decoding, without projection or transport."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_DOC_ID_RE = re.compile(r"S[0-9A-Z]{3,32}")


def safe_error_value(value: object, *, max_length: int = 80) -> str:
    text = str(value)
    sanitized = "".join(
        char if char.isprintable() and char not in "\r\n\t" else "?" for char in text
    )
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."
    return repr(sanitized)


def coalesce_field(record: Mapping[str, Any], *keys: str) -> Any:
    """Return first key with a non-None/non-empty value, preserving legitimate zeros."""
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def text_or_none(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def parse_doc_id(value: object) -> str:
    raw = str(value or "").strip()
    if not raw.isascii() or not _DOC_ID_RE.fullmatch(raw.upper()):
        raise ValueError(f"invalid EDINET docID: {safe_error_value(value)}")
    return raw.upper()


def validate_document_statuses(document: Mapping[str, Any]) -> None:
    allowed = {
        "legal": {None, "0", "1", "2"},
        "withdrawal": {None, "0", "1", "2"},
        "edit": {None, "0", "1", "2"},
        "disclosure": {None, "0", "1", "2", "3"},
    }
    values = {
        "legal": text_or_none(coalesce_field(document, "legalStatus", "legal_status")),
        "withdrawal": text_or_none(
            coalesce_field(document, "withdrawalStatus", "withdrawal_status")
        ),
        "edit": text_or_none(coalesce_field(document, "docInfoEditStatus", "doc_info_edit_status")),
        "disclosure": text_or_none(
            coalesce_field(document, "disclosureStatus", "disclosure_status")
        ),
    }
    for name, value in values.items():
        if value not in allowed[name]:
            raise ValueError(f"unknown EDINET {name} status: {value!r}")
