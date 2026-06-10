"""Candidate tag and summary rendering for selection payload output."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from ._coerce import (
    _dedupe_strings,
    _int_or,
    _string_sequence,
    _string_value,
)
from .lenses import _fast_guard_count, _fast_lens, _long_hold_lens


def _long_hold_counts(candidates: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for candidate in candidates:
        rating = _string_value(_long_hold_lens(candidate).get("rating")) or "unknown"
        counts[rating] += 1
    return dict(counts)


def _candidate_reason_tags(candidate: Mapping[str, object]) -> list[str]:
    tags: list[str] = []
    if _fast_lens(candidate).get("eligible") is True:
        tags.append("fast_dislocation")
    lane = _string_value(candidate.get("selection_lane"))
    if lane:
        tags.append(lane)
    long_hold = _long_hold_lens(candidate)
    rating = _string_value(long_hold.get("rating"))
    if rating == "high":
        tags.append("long_hold_high")
    elif rating == "medium":
        tags.append("long_hold_medium")
    fast = _fast_lens(candidate)
    confidence = _string_value(fast.get("confidence"))
    if confidence == "high":
        tags.append("fast_confidence_high")
    return _dedupe_strings(tags)


def _candidate_risk_tags(candidate: Mapping[str, object]) -> list[str]:
    tags: list[str] = []
    if candidate.get("previous_candidate") is True:
        tags.append("previous_candidate")
    if candidate.get("suppressed") is True:
        tags.append("suppressed_by_prior_research")
    if _string_value(candidate.get("next_earnings_date")):
        tags.append("earnings_scheduled")
    if candidate.get("freshness_warnings"):
        tags.append("freshness_warning")
    fast = _fast_lens(candidate)
    if fast.get("stale_fundamental_metrics") is True:
        tags.append("stale_fundamental_metrics")
    return _dedupe_strings(tags)


def _selection_candidate_summary(
    candidate: Mapping[str, object], *, rank: int
) -> dict[str, object]:
    fast_lens = _fast_lens(candidate)
    long_hold_lens = _long_hold_lens(candidate)
    return {
        "rank": rank,
        "ticker": _string_value(candidate.get("ticker")),
        "name": _string_value(candidate.get("name")),
        "sector_33": _string_value(candidate.get("sector_33")),
        "selection_lane": _string_value(candidate.get("selection_lane")),
        "macro_context_alignment": _string_value(candidate.get("macro_context_alignment")),
        "market_cap_oku": candidate.get("market_cap_oku"),
        "price_change_5d": candidate.get("price_change_5d"),
        "price_change_20d": candidate.get("price_change_20d"),
        "gap_from_52w_low": candidate.get("gap_from_52w_low"),
        "next_earnings_date": candidate.get("next_earnings_date"),
        "position_tier": candidate.get("position_tier"),
        "fast_confidence": _string_value(fast_lens.get("confidence")),
        "fast_guard_count": _fast_guard_count(candidate),
        "fast_guard_family_count": _int_or(fast_lens.get("fundamental_guard_family_count"), 0),
        "fast_data_status": _string_value(fast_lens.get("data_status")),
        "long_hold_rating": _string_value(long_hold_lens.get("rating")),
        "prior_research": candidate.get("prior_research"),
        "previous_candidate": candidate.get("previous_candidate") is True,
        "suppressed": candidate.get("suppressed") is True,
        "suppression_reasons": list(_string_sequence(candidate.get("suppression_reasons"))),
        "reason_tags": list(_string_sequence(candidate.get("reason_tags"))),
        "risk_tags": list(_string_sequence(candidate.get("risk_tags"))),
    }


def _sweep_candidate_summary(candidate: Mapping[str, object], *, rank: int) -> dict[str, object]:
    fast_lens = _fast_lens(candidate)
    long_hold_lens = _long_hold_lens(candidate)
    return {
        "rank": rank,
        "ticker": _string_value(candidate.get("ticker")),
        "name": _string_value(candidate.get("name")),
        "selection_lane": _string_value(candidate.get("selection_lane")),
        "fast_confidence": _string_value(fast_lens.get("confidence")),
        "fast_guard_count": _fast_guard_count(candidate),
        "fast_guard_family_count": _int_or(fast_lens.get("fundamental_guard_family_count"), 0),
        "fast_data_status": _string_value(fast_lens.get("data_status")),
        "stale_fundamental_metrics": fast_lens.get("stale_fundamental_metrics") is True,
        "long_hold_rating": _string_value(long_hold_lens.get("rating")),
        "previous_candidate": candidate.get("previous_candidate") is True,
        "reason_tags": list(_string_sequence(candidate.get("reason_tags"))),
        "risk_tags": list(_string_sequence(candidate.get("risk_tags"))),
    }


def _sweep_changed_summaries(
    base_by_ticker: Mapping[str, Mapping[str, object]],
    current_by_ticker: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    changed: list[dict[str, object]] = []
    for ticker in sorted(set(base_by_ticker) & set(current_by_ticker)):
        base = base_by_ticker[ticker]
        current = current_by_ticker[ticker]
        changed_fields = {
            key
            for key in (
                "fast_confidence",
                "fast_guard_count",
                "fast_guard_family_count",
                "fast_data_status",
                "long_hold_rating",
                "selection_lane",
                "rank",
                "stale_fundamental_metrics",
            )
            if base.get(key) != current.get(key)
        }
        if not changed_fields:
            continue
        changed.append(
            {
                "ticker": ticker,
                "changed_fields": sorted(changed_fields),
                "from": {key: base.get(key) for key in sorted(changed_fields)},
                "to": {key: current.get(key) for key in sorted(changed_fields)},
            }
        )
    return changed
