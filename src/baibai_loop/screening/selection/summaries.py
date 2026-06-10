"""Candidate tag and summary rendering for selection payload output."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence

from baibai_loop.coerce import (
    dedupe_strings,
    int_or,
    string_or_none,
    string_sequence,
)

from .lenses import _fast_guard_count, _fast_lens, _long_hold_lens


def _long_hold_counts(candidates: Sequence[Mapping[str, object]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for candidate in candidates:
        rating = string_or_none(_long_hold_lens(candidate).get("rating")) or "unknown"
        counts[rating] += 1
    return dict(counts)


def _candidate_reason_tags(candidate: Mapping[str, object]) -> list[str]:
    tags: list[str] = []
    if _fast_lens(candidate).get("eligible") is True:
        tags.append("fast_dislocation")
    lane = string_or_none(candidate.get("selection_lane"))
    if lane:
        tags.append(lane)
    long_hold = _long_hold_lens(candidate)
    rating = string_or_none(long_hold.get("rating"))
    if rating == "high":
        tags.append("long_hold_high")
    elif rating == "medium":
        tags.append("long_hold_medium")
    fast = _fast_lens(candidate)
    confidence = string_or_none(fast.get("confidence"))
    if confidence == "high":
        tags.append("fast_confidence_high")
    return dedupe_strings(tags)


def _candidate_risk_tags(candidate: Mapping[str, object]) -> list[str]:
    tags: list[str] = []
    if candidate.get("previous_candidate") is True:
        tags.append("previous_candidate")
    if candidate.get("suppressed") is True:
        tags.append("suppressed_by_prior_research")
    if string_or_none(candidate.get("next_earnings_date")):
        tags.append("earnings_scheduled")
    if candidate.get("freshness_warnings"):
        tags.append("freshness_warning")
    fast = _fast_lens(candidate)
    if fast.get("stale_fundamental_metrics") is True:
        tags.append("stale_fundamental_metrics")
    return dedupe_strings(tags)


def _selection_candidate_summary(
    candidate: Mapping[str, object], *, rank: int
) -> dict[str, object]:
    fast_lens = _fast_lens(candidate)
    long_hold_lens = _long_hold_lens(candidate)
    return {
        "rank": rank,
        "ticker": string_or_none(candidate.get("ticker")),
        "name": string_or_none(candidate.get("name")),
        "sector_33": string_or_none(candidate.get("sector_33")),
        "selection_lane": string_or_none(candidate.get("selection_lane")),
        "macro_context_alignment": string_or_none(candidate.get("macro_context_alignment")),
        "market_cap_oku": candidate.get("market_cap_oku"),
        "price_change_5d": candidate.get("price_change_5d"),
        "price_change_20d": candidate.get("price_change_20d"),
        "gap_from_52w_low": candidate.get("gap_from_52w_low"),
        "next_earnings_date": candidate.get("next_earnings_date"),
        "position_tier": candidate.get("position_tier"),
        "fast_confidence": string_or_none(fast_lens.get("confidence")),
        "fast_guard_count": _fast_guard_count(candidate),
        "fast_guard_family_count": int_or(fast_lens.get("fundamental_guard_family_count"), 0),
        "fast_data_status": string_or_none(fast_lens.get("data_status")),
        "long_hold_rating": string_or_none(long_hold_lens.get("rating")),
        "prior_research": candidate.get("prior_research"),
        "previous_candidate": candidate.get("previous_candidate") is True,
        "suppressed": candidate.get("suppressed") is True,
        "suppression_reasons": list(string_sequence(candidate.get("suppression_reasons"))),
        "reason_tags": list(string_sequence(candidate.get("reason_tags"))),
        "risk_tags": list(string_sequence(candidate.get("risk_tags"))),
    }


def _sweep_candidate_summary(candidate: Mapping[str, object], *, rank: int) -> dict[str, object]:
    fast_lens = _fast_lens(candidate)
    long_hold_lens = _long_hold_lens(candidate)
    return {
        "rank": rank,
        "ticker": string_or_none(candidate.get("ticker")),
        "name": string_or_none(candidate.get("name")),
        "selection_lane": string_or_none(candidate.get("selection_lane")),
        "fast_confidence": string_or_none(fast_lens.get("confidence")),
        "fast_guard_count": _fast_guard_count(candidate),
        "fast_guard_family_count": int_or(fast_lens.get("fundamental_guard_family_count"), 0),
        "fast_data_status": string_or_none(fast_lens.get("data_status")),
        "stale_fundamental_metrics": fast_lens.get("stale_fundamental_metrics") is True,
        "long_hold_rating": string_or_none(long_hold_lens.get("rating")),
        "previous_candidate": candidate.get("previous_candidate") is True,
        "reason_tags": list(string_sequence(candidate.get("reason_tags"))),
        "risk_tags": list(string_sequence(candidate.get("risk_tags"))),
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
