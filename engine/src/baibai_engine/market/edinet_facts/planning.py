"""Classify one selected annual filing before acquiring or extracting its archive."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from baibai_engine.market.edinet_decode import parse_sec_code
from baibai_engine.market.edinet_facts.extract import EXTRACTOR_REVISION
from baibai_engine.market.edinet_facts.models import FactIdentity, filing_identity
from baibai_engine.market.edinet_facts.selection import usable_document


@dataclass(frozen=True, slots=True)
class FactExtractionItem:
    doc_id: str
    submitted: str
    identity: FactIdentity | None
    action: Literal["unknown_submission", "future", "unavailable", "reused", "extract"]
    reason: str | None = None


def plan_fact_item(
    doc: dict[str, Any],
    *,
    statuses: Mapping[str, tuple[str, str | None]],
    asof: date,
) -> FactExtractionItem:
    doc_id = str(doc["docID"])
    submitted = str(doc.get("submitDateTime") or "")
    try:
        identity = filing_identity(parse_sec_code(doc["secCode"]), doc_id, submitted)
    except ValueError:
        return FactExtractionItem(doc_id, submitted, None, "unknown_submission")
    if date.fromisoformat(identity.disclosed_on) > asof:
        return FactExtractionItem(doc_id, submitted, identity, "future")
    if not usable_document(doc):
        reason = (
            "annual_period_unknown"
            if not doc.get("_research_period_known", True)
            else "document_validity_or_xbrl_unknown"
        )
        return FactExtractionItem(doc_id, submitted, identity, "unavailable", reason)
    previous = statuses.get(doc_id)
    if previous and previous[0] == "ok":
        cached = json.loads(previous[1] or "{}")
        if (
            cached.get("revision") == EXTRACTOR_REVISION
            and cached.get("identity") == identity.model_dump()
        ):
            return FactExtractionItem(doc_id, submitted, identity, "reused")
    return FactExtractionItem(doc_id, submitted, identity, "extract")
