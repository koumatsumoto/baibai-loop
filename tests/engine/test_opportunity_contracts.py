from __future__ import annotations

from baibai_engine.screening.selection.contracts import value_carry_selection_policy_hash


def _hash(
    *,
    rules_hash: str = "rules-a",
    min_market_cap_oku: float = 100.0,
    evidence_pattern_order: tuple[str, ...] = ("cash-rich-asset-discount",),
) -> str:
    return value_carry_selection_policy_hash(
        lane_longlist_depth=20,
        screening_rules_hash=rules_hash,
        required_jpx_flags=("監理銘柄",),
        liquidity_parameters={
            "min_market_cap_oku": min_market_cap_oku,
            "min_avg_turnover_oku": 1.0,
            "min_listing_span_days": 182,
            "exclude_jpx_flagged": True,
        },
        evidence_pattern_order=evidence_pattern_order,
    )


def test_value_carry_policy_hash_is_deterministic_for_the_same_exact_inputs() -> None:
    assert _hash() == _hash()


def test_value_carry_policy_hash_changes_with_membership_or_order_inputs() -> None:
    baseline = _hash()

    assert _hash(rules_hash="rules-b") != baseline
    assert _hash(min_market_cap_oku=200.0) != baseline
    assert _hash(evidence_pattern_order=("valuation-reversion",)) != baseline
