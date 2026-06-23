"""Code-owned portfolio policy thresholds used by validators."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

type PolicyConfig = Mapping[str, Any]

PORTFOLIO_POLICY: dict[str, Any] = {
    "capital_basis": {
        "real_capital_yen": 5_000_000,
        "tactical_real_budget_yen": 2_000_000,
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
    "order_constraints": {"board_lot": 100, "price_guard_required": True},
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
    ("order_constraints", "board_lot"),
)


def validate_policy(policy: PolicyConfig = PORTFOLIO_POLICY) -> None:
    """Fail fast when a code-owned policy threshold is missing or mistyped."""

    for path in _REQUIRED_NUMERIC_PATHS:
        value = _value_at(policy, path)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            dotted = ".".join(path)
            raise RuntimeError(f"PORTFOLIO_POLICY.{dotted} must be numeric")


def _value_at(policy: PolicyConfig, path: tuple[str, ...]) -> object:
    current: object = policy
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


validate_policy()
