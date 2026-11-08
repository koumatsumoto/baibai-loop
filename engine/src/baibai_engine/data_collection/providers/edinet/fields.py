"""Native EDINET identity and status decoding, without projection or transport."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
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


def is_usable_filing_status(legal: object, disclosure: object, withdrawal: object) -> bool:
    """Require explicit published, non-withdrawn status; missing flags are unknown."""
    return str(legal) in {"1", "2"} and str(disclosure) == "0" and str(withdrawal) == "0"


def parse_optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


_DOCUMENT_DESCRIPTION_PERIOD_RE = re.compile(
    r"(\d{4}/\d{2}/\d{2})\s*[－~～-]\s*(\d{4}/\d{2}/\d{2})"
)


def document_period(document: Mapping[str, Any]) -> tuple[date | None, date | None]:
    start = parse_optional_date(coalesce_field(document, "periodStart", "period_start"))
    end = parse_optional_date(coalesce_field(document, "periodEnd", "period_end"))
    if start is not None and end is not None:
        return start, end
    description = text_or_none(coalesce_field(document, "docDescription", "doc_description"))
    fallback_start, fallback_end = _parse_document_description_period(description)
    return start or fallback_start, end or fallback_end


def _parse_document_description_period(value: str | None) -> tuple[date | None, date | None]:
    if not value:
        return None, None
    match = _DOCUMENT_DESCRIPTION_PERIOD_RE.search(value)
    if match is None:
        return None, None
    return (
        _parse_slash_date(match.group(1)),
        _parse_slash_date(match.group(2)),
    )


def _parse_slash_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value.replace("/", "-"))
    except ValueError:
        return None
