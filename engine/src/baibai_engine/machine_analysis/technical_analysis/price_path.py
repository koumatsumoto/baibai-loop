"""Common-calendar price observations and a finite research annotation, without I/O."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import isfinite

from baibai_engine.market.price_models import PriceBar
from baibai_engine.market.price_observation import (
    PriceCalendar,
    PriceComparisonPair,
    PriceDayState,
)

PRICE_PATH_REVISION = "common-calendar-observed-close-path-v1"
ANNOTATION_POLICY_REVISION = "research-price-note-b-v1"
_THRESHOLDS = {"R1": -0.08, "R20": -0.20, "G60": -0.20}
_STATES = ("priced", "no_trade", "absent", "mixed", "invalid")


@dataclass(frozen=True, slots=True)
class PricePath:
    payload: dict[str, object]
    comparison_pairs: tuple[PriceComparisonPair, ...]


def observe_price_path(
    bars: Sequence[PriceBar],
    *,
    calendar: PriceCalendar,
    day_states: Mapping[date, PriceDayState],
) -> PricePath:
    """Use observed prices inside fixed calendar windows, never an old current price.

    Internal holes do not remove an exact endpoint return. For a peak observation,
    a real pair can prove a threshold match even with unknown days; a non-match
    requires every date to be accounted for and at least one past price.
    """
    retained = {
        bar.traded_at: bar.price
        for bar in bars
        if bar.traded_at <= calendar.as_of and isfinite(bar.price) and bar.price > 0
    }
    prices = {day: value for day, value in retained.items() if day_states.get(day) == "priced"}
    expected = calendar.expected_price_date
    current = None if expected is None else prices.get(expected)
    latest = max(retained, default=None)
    observations: dict[str, object] = {}
    pairs: list[PriceComparisonPair] = []
    decisions: dict[str, bool | None] = {}
    for differences, window in calendar.windows.items():
        ref = f"R{differences}" if differences in (1, 20) else f"G{differences}"
        coverage = _coverage(window, day_states=day_states, prices=prices)
        item: dict[str, object] = {
            "kind": "endpoint_return" if ref.startswith("R") else "observed_peak_to_current",
            "calendar_differences": differences,
            "window_start_date": None if window is None else window[0].isoformat(),
            "window_end_date": None if window is None else window[-1].isoformat(),
            "coverage": coverage,
        }
        start_date = None if window is None else window[0]
        start_price = None if start_date is None else prices.get(start_date)
        reason = (
            "insufficient_calendar"
            if window is None
            else "missing_current_price"
            if current is None
            else None
        )
        if ref.startswith("G"):
            observed = (
                [] if window is None else [(day, prices[day]) for day in window if day in prices]
            )
            peak = max(observed, key=lambda row: (row[1], row[0]), default=None)
            past_count = sum(day != expected for day, _ in observed)
            item.update(
                observed_peak_date=None if peak is None else peak[0].isoformat(),
                observed_peak_price=None if peak is None else peak[1],
                window_start_price=start_price,
                window_return=_return(start_price, current) if window is not None else None,
                window_return_unavailable_reason=reason
                or ("missing_window_start_price" if start_price is None else None),
            )
            start_date, start_price = (None, None) if peak is None else peak
            if reason is None and not past_count:
                reason = "no_past_price"
        elif reason is None and start_price is None:
            reason = "missing_start_price"
        value = None if reason is not None else _return(start_price, current)
        item.update(
            comparison_start_date=None if start_date is None else start_date.isoformat(),
            comparison_end_date=None if expected is None else expected.isoformat(),
            comparison_start_price=start_price,
            comparison_end_price=current,
            value=value,
            unavailable_reason=reason,
        )
        observations[ref] = item
        if value is not None:
            assert start_date is not None and expected is not None
            assert start_price is not None and current is not None
            pairs.append(PriceComparisonPair(ref, start_date, expected, start_price, current))
        if ref in _THRESHOLDS:
            decision: bool | None = None
            if value is not None:
                assert start_price is not None and current is not None
                matched = _at_or_below(start_price, current, _THRESHOLDS[ref])
                if matched:
                    decision = True
                elif ref.startswith("R") or coverage["complete"]:
                    decision = False
            decisions[ref] = decision
    matched_refs = [ref for ref, decision in decisions.items() if decision is True]
    unavailable_refs = [ref for ref, decision in decisions.items() if decision is None]
    return PricePath(
        payload={
            "calculation_revision": PRICE_PATH_REVISION,
            "as_of": calendar.as_of.isoformat(),
            "price_basis": "price_adjusted_close",
            "adjustment_as_of": calendar.as_of.isoformat(),
            "row_state_basis": "canonical_daily_ohlcv_all_six",
            "no_trade_reason": "no_executions_ordinary_no_trade_or_full_day_suspension_unresolved",
            "read_semantics": "retained_prices_at_date_cutoff_not_original_publication_vintage",
            "expected_price_date": None if expected is None else expected.isoformat(),
            "latest_observed_price_date": None if latest is None else latest.isoformat(),
            "current_price": current,
            "current_row_state": None if expected is None else _state(expected, day_states, prices),
            "freshness": "unknown_calendar"
            if expected is None
            else "current"
            if current is not None
            else "missing_expected_close",
            "observations": observations,
            "annotation": {
                "policy_revision": ANNOTATION_POLICY_REVISION,
                "thresholds": dict(_THRESHOLDS),
                "comparison": "inclusive_decimal_cross_product_on_adjusted_float_strings",
                "status": "matched"
                if matched_refs
                else "unassessable"
                if unavailable_refs
                else "not_matched",
                "matched_refs": matched_refs,
                "evaluated_refs": [
                    ref for ref, decision in decisions.items() if decision is not None
                ],
                "unavailable_refs": unavailable_refs,
            },
        },
        comparison_pairs=tuple(pairs),
    )


def _at_or_below(start: float, end: float, threshold: float) -> bool:
    # Avoid binary division at exact boundaries (100 -> 80 is a -20% match).
    # These are the existing adjusted floats; this does not round observations or
    # claim to recover precision already lost in the upstream factor calculation.
    return Decimal(str(end)) <= Decimal(str(start)) * (Decimal(1) + Decimal(str(threshold)))


def _return(start: float | None, end: float | None) -> float | None:
    return None if start is None or end is None else end / start - 1


def _state(
    day: date, day_states: Mapping[date, PriceDayState], prices: Mapping[date, float]
) -> str:
    state = day_states.get(day, "absent")
    return "invalid" if state == "priced" and day not in prices else state


def _coverage(
    window: tuple[date, ...] | None,
    *,
    day_states: Mapping[date, PriceDayState],
    prices: Mapping[date, float],
) -> dict[str, object]:
    if window is None:
        return {"calendar_status": "unavailable", "row_counts": None, "complete": False}
    states = {day: _state(day, day_states, prices) for day in window}
    counts = Counter(states.values())
    return {
        "calendar_status": "available",
        "expected_dates": len(window),
        "row_counts": {state: counts[state] for state in _STATES},
        "unpriced_dates": {
            state: [day.isoformat() for day, actual in states.items() if actual == state]
            for state in _STATES[1:]
            if counts[state]
        },
        "complete": not any(counts[state] for state in ("absent", "mixed", "invalid")),
    }
