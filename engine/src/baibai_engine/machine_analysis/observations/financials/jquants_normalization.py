"""Normalize native J-Quants financial records without a storage dependency."""

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from baibai_engine.machine_analysis.observations.financials.financial_documents import (
    required_disclosure_number,
)
from baibai_engine.machine_analysis.observations.jquants_decode import provider_record_payload
from baibai_engine.machine_analysis.observations.normalization import (
    NormalizedRows,
    code_quality,
)


def normalize_financial_summaries(records: Iterable[Mapping[str, Any]]) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    seen: dict[tuple[str, str, str], str] = {}
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, quality = code_quality(record.get("Code"), require_text=True)
        if quality == "excluded":
            excluded_count += 1
            continue
        disclosed_at = _disclosure_day(record.get("DiscDate"))
        if ticker is None or disclosed_at is None:
            rejected_count += 1
            continue
        # Store provider fields, not selected forecasts or a second numeric truth.
        # Source record JSON is not the original HTTP response or Bulk CSV bytes.
        key = (ticker, disclosed_at, required_disclosure_number(record))
        payload = provider_record_payload(record)
        if key in seen:
            if seen[key] != payload:
                raise ValueError(f"conflicting financial document identity: {key}")
            continue
        seen[key] = payload
        rows.append((*key, payload))
    return NormalizedRows(rows=rows, rejected_count=rejected_count, excluded_count=excluded_count)


def _disclosure_day(raw: object) -> str | None:
    """A TDnet disclosure date, not a timestamp or an accounting period end."""
    if not isinstance(raw, str):
        return None
    value = raw.removesuffix("T00:00:00")
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None
