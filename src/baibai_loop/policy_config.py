"""Code-owned portfolio policy thresholds used by validators."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

type PolicyConfig = Mapping[str, Any]

PORTFOLIO_POLICY: dict[str, Any] = {
    "capital_basis": {
        "real_capital_yen": 5_000_000,
        "tactical_real_budget_yen": 1_000_000,
        "paper_proxy_capital_yen": 100_000_000,
    },
    "risk_budget": {
        "max_paper_proxy_position_size_yen": 2_000_000,
        "max_real_order_notional_yen": 500_000,
        "max_ticker_real_concentration_pct": 8.0,
        "max_sector_real_concentration_pct": 45.0,
        "max_playbook_real_concentration_pct": 35.0,
        "max_adv_participation_pct": 5.0,
    },
    "execution_scaling": {"paper_to_real_order_notional_pct": 21.0},
    "conviction_tier_caps": {
        "low": {
            "max_paper_proxy_position_size_yen": 500_000,
            "max_real_order_notional_yen": 100_000,
        },
        "medium": {
            "max_paper_proxy_position_size_yen": 1_000_000,
            "max_real_order_notional_yen": 250_000,
        },
        "high": {
            "max_paper_proxy_position_size_yen": 2_000_000,
            "max_real_order_notional_yen": 500_000,
        },
    },
    "evidence_count_caps": {
        "count_1": {
            "max_real_order_notional_yen": 75_000,
            "requires_disconfirming_or_risk_evidence": True,
            "requires_payoff_confirmation": True,
        },
    },
    "conviction_tier_rules": {
        "count_breadth": {
            "high_min_independent_evidence_count": 3,
            "medium_min_independent_evidence_count": 1,
        },
        "high_depth": {
            "min_independent_evidence_count": 1,
            "min_risk_reward_ratio": 2.0,
            "requires_depth_verification_ref": True,
            "requires_disconfirming_or_risk_evidence": True,
        },
    },
    "sizing_ladder": {
        "low": {"default_paper_proxy_position_size_yen": 500_000},
        "medium": {"default_paper_proxy_position_size_yen": 1_000_000},
        "high": {"default_paper_proxy_position_size_yen": 1_500_000},
    },
    "order_constraints": {"board_lot": 100, "price_guard_required": True},
    "kill_switch": {
        "earnings_straddle": {"validator_callable_id": "earnings_straddle_window"},
        "boj_eve": {"validator_callable_id": "boj_eve_window"},
        "fomc_eve": {"validator_callable_id": "fomc_eve_window"},
    },
    "unique_constraints": [
        {"id": "no-margin-trading", "validator_callable_id": "no_margin_trading"},
    ],
}

_REQUIRED_NUMERIC_PATHS: tuple[tuple[str, ...], ...] = (
    ("capital_basis", "real_capital_yen"),
    ("capital_basis", "tactical_real_budget_yen"),
    ("capital_basis", "paper_proxy_capital_yen"),
    ("risk_budget", "max_paper_proxy_position_size_yen"),
    ("risk_budget", "max_real_order_notional_yen"),
    ("risk_budget", "max_ticker_real_concentration_pct"),
    ("risk_budget", "max_sector_real_concentration_pct"),
    ("risk_budget", "max_playbook_real_concentration_pct"),
    ("risk_budget", "max_adv_participation_pct"),
    ("execution_scaling", "paper_to_real_order_notional_pct"),
    ("conviction_tier_caps", "low", "max_paper_proxy_position_size_yen"),
    ("conviction_tier_caps", "low", "max_real_order_notional_yen"),
    ("conviction_tier_caps", "medium", "max_paper_proxy_position_size_yen"),
    ("conviction_tier_caps", "medium", "max_real_order_notional_yen"),
    ("conviction_tier_caps", "high", "max_paper_proxy_position_size_yen"),
    ("conviction_tier_caps", "high", "max_real_order_notional_yen"),
    ("evidence_count_caps", "count_1", "max_real_order_notional_yen"),
    ("conviction_tier_rules", "count_breadth", "high_min_independent_evidence_count"),
    ("conviction_tier_rules", "count_breadth", "medium_min_independent_evidence_count"),
    ("conviction_tier_rules", "high_depth", "min_independent_evidence_count"),
    ("conviction_tier_rules", "high_depth", "min_risk_reward_ratio"),
    ("sizing_ladder", "low", "default_paper_proxy_position_size_yen"),
    ("sizing_ladder", "medium", "default_paper_proxy_position_size_yen"),
    ("sizing_ladder", "high", "default_paper_proxy_position_size_yen"),
    ("order_constraints", "board_lot"),
)


def validate_policy_config(policy: PolicyConfig = PORTFOLIO_POLICY) -> None:
    """Fail fast when a code-owned policy threshold is missing or mistyped."""

    for path in _REQUIRED_NUMERIC_PATHS:
        value = _value_at(policy, path)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            dotted = ".".join(path)
            raise RuntimeError(f"PORTFOLIO_POLICY.{dotted} must be numeric")
    kill_switch = policy.get("kill_switch")
    if not isinstance(kill_switch, Mapping) or not kill_switch:
        raise RuntimeError("PORTFOLIO_POLICY.kill_switch must be a non-empty mapping")
    for key, item in kill_switch.items():
        if not isinstance(item, Mapping) or not isinstance(item.get("validator_callable_id"), str):
            raise RuntimeError(
                f"PORTFOLIO_POLICY.kill_switch.{key}.validator_callable_id must be a string"
            )
    unique_constraints = policy.get("unique_constraints")
    if not isinstance(unique_constraints, list) or not unique_constraints:
        raise RuntimeError("PORTFOLIO_POLICY.unique_constraints must be a non-empty list")
    for index, item in enumerate(unique_constraints):
        if not isinstance(item, Mapping) or not isinstance(item.get("validator_callable_id"), str):
            raise RuntimeError(
                "PORTFOLIO_POLICY.unique_constraints"
                f"[{index}].validator_callable_id must be a string"
            )


def _value_at(policy: PolicyConfig, path: tuple[str, ...]) -> object:
    current: object = policy
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


validate_policy_config()
