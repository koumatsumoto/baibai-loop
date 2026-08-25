"""The run payload `ScreeningRunStore.publish_run` accepts, in one place.

Every test that needs a stored run was writing its own literal, so a new required
field, or a change to what the store validates, had to be found in nine files. The
builder emits a payload the writer accepts; what a test varies it passes in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


class _Omitted:
    """Distinguishes "not asked for" from an explicit `None`."""


OMIT = _Omitted()

RULES_HASH = "rules-fixture"
MODEL_ID = "expected-return-v1"


def evidence_hit(
    name: str = "valuation-reversion",
    *,
    evidence_pattern_id: str = "cashflow-yield-discount",
    source_status: str = "ok",
    sizing_eligible: bool = True,
) -> dict[str, Any]:
    """One evidence hit with the four keys `run_store` requires of every hit.

    A hit carrying only its name is a shape the store refuses, so a fixture that
    builds one is describing a run that could never have been published.
    """

    return {
        "name": name,
        "evidence_pattern_id": evidence_pattern_id,
        "source_status": source_status,
        "sizing_eligible": sizing_eligible,
    }


def screening_candidate(
    ticker: str = "1301",
    *,
    name: str = "極洋",
    sector_33: str = "水産・農林業",
    market_cap_oku: float = 1000.0,
    avg_turnover_oku: float = 10.0,
    listing_span_days: int = 1000,
    jpx_flags: Sequence[str] = (),
    metrics: Mapping[str, Any] | _Omitted | None = OMIT,
    evidence_hits: Sequence[Mapping[str, Any]] = (),
    **extra: Any,
) -> dict[str, Any]:
    """One candidate row of a run payload.

    `metrics` has three states the store distinguishes and so does this: left out it
    carries the default reading, `{}` carries none, and `None` leaves the key off the
    payload entirely — which is a shape the store accepts and a reader has to survive.
    """

    payload: dict[str, Any] = {
        "ticker": ticker,
        "name": name,
        "sector_33": sector_33,
        "market_cap_oku": market_cap_oku,
        "avg_turnover_oku": avg_turnover_oku,
        "listing_span_days": listing_span_days,
        "jpx_flags": list(jpx_flags),
        "evidence_hits": [dict(hit) for hit in evidence_hits],
        **extra,
    }
    if metrics is OMIT:
        payload["metrics"] = {"er_annual": 0.13}
    elif metrics is not None:
        payload["metrics"] = dict(metrics)
    return payload


def screening_run_payload(
    *,
    as_of: str = "2026-07-08",
    run_at: str | None = None,
    run_id: str | None = None,
    universe_size: int = 3744,
    rules_hash: str = RULES_HASH,
    model_id: str = MODEL_ID,
    candidates: Sequence[Mapping[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A complete run payload for `as_of`, ready to publish."""

    payload: dict[str, Any] = {
        "run_id": run_id or f"screening-{as_of.replace('-', '')}",
        "run_date": as_of,
        "asof_date": as_of,
        "run_at": run_at or f"{as_of}T18:00:00+09:00",
        "universe_size": universe_size,
        "screening_rules_hash": rules_hash,
        "er_model_version": model_id,
        "candidates": [dict(candidate) for candidate in (candidates or [screening_candidate()])],
    }
    payload.update(extra)
    return payload
