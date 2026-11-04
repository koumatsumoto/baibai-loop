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
    date_iso,
)


def normalize_financial_summaries(records: Iterable[Mapping[str, Any]]) -> NormalizedRows:
    rows: list[tuple[Any, ...]] = []
    seen: dict[tuple[str, str, str], str] = {}
    rejected_count = 0
    excluded_count = 0
    for record in records:
        ticker, quality = code_quality(record.get("Code"))
        if quality == "rejected":
            rejected_count += 1
            continue
        if quality == "excluded":
            excluded_count += 1
            continue
        disclosed_at = date_iso(record.get("DiscDate"))
        if ticker is None or disclosed_at is None:
            rejected_count += 1
            continue
        disclosed_at = date.fromisoformat(disclosed_at).isoformat()
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
