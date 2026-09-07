"""Code-owned portfolio policy thresholds used by validators."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

type PolicyConfig = Mapping[str, Any]

# 資本額は ledger event から再計算する。ここには状態を置かず、判断時に適用する
# warning line と注文制約だけを置く。
PORTFOLIO_POLICY: dict[str, Any] = {
    "cash_management": {
        "dry_powder_warning_pct": 20.0,
        "override_max_days": 31,
    },
    "risk_budget": {
        "max_ticker_concentration_pct": 10.0,
        "max_sector_concentration_pct": 40.0,
        "max_common_factor_concentration_pct": 35.0,
        "max_adv_participation_pct": 5.0,
    },
    "order_constraints": {"board_lot": 100, "price_guard_required": True},
    "valuation": {
        "market_price_max_age_days": 7,
        # Position size does not repair an insufficient expected return.
        "minimum_required_annual_return_pct": 8.5,
    },
}

_REQUIRED_NUMERIC_PATHS: tuple[tuple[str, ...], ...] = (
    ("cash_management", "dry_powder_warning_pct"),
    ("cash_management", "override_max_days"),
    ("risk_budget", "max_ticker_concentration_pct"),
    ("risk_budget", "max_sector_concentration_pct"),
    ("risk_budget", "max_common_factor_concentration_pct"),
    ("risk_budget", "max_adv_participation_pct"),
    ("order_constraints", "board_lot"),
    ("valuation", "market_price_max_age_days"),
    ("valuation", "minimum_required_annual_return_pct"),
)


def validate_policy(policy: PolicyConfig = PORTFOLIO_POLICY) -> None:
    """Fail fast when a code-owned policy threshold is missing or mistyped."""

    for path in _REQUIRED_NUMERIC_PATHS:
        value = _value_at(policy, path)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            dotted = ".".join(path)
            raise RuntimeError(f"PORTFOLIO_POLICY.{dotted} must be numeric")
    positive_integer_paths = (
        ("cash_management", "override_max_days"),
        ("order_constraints", "board_lot"),
        ("valuation", "market_price_max_age_days"),
    )
    for path in positive_integer_paths:
        value = _value_at(policy, path)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError(f"PORTFOLIO_POLICY.{'.'.join(path)} must be a positive integer")
    percentage_paths = (
        ("cash_management", "dry_powder_warning_pct"),
        ("risk_budget", "max_ticker_concentration_pct"),
        ("risk_budget", "max_sector_concentration_pct"),
        ("risk_budget", "max_common_factor_concentration_pct"),
        ("risk_budget", "max_adv_participation_pct"),
        ("valuation", "minimum_required_annual_return_pct"),
    )
    for path in percentage_paths:
        value = _value_at(policy, path)
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 100:
            raise RuntimeError(f"PORTFOLIO_POLICY.{'.'.join(path)} must be within 0..100")
    price_guard_required = _value_at(policy, ("order_constraints", "price_guard_required"))
    if not isinstance(price_guard_required, bool):
        raise RuntimeError(
            "PORTFOLIO_POLICY.order_constraints.price_guard_required must be boolean"
        )


def _value_at(policy: PolicyConfig, path: tuple[str, ...]) -> object:
    current: object = policy
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


validate_policy()
