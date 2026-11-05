"""Code-owned portfolio policy thresholds used by validators."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict


class CashManagementPolicy(TypedDict):
    dry_powder_warning_pct: float
    override_max_days: int


class RiskBudgetPolicy(TypedDict):
    max_ticker_concentration_pct: float
    max_sector_concentration_pct: float
    max_common_factor_concentration_pct: float
    max_adv_participation_pct: float


class OrderConstraints(TypedDict):
    board_lot: int


class ValuationPolicy(TypedDict):
    market_price_max_age_days: int


class PolicyConfig(TypedDict):
    cash_management: CashManagementPolicy
    risk_budget: RiskBudgetPolicy
    order_constraints: OrderConstraints
    valuation: ValuationPolicy


# 資本額は ledger event から再計算する。ここには状態を置かず、判断時に適用する
# warning line と注文制約だけを置く。
# 保存済み提案の policy hash を変えないため、型を付けても辞書の表現を保つ。
PORTFOLIO_POLICY: PolicyConfig = {
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
    "order_constraints": {"board_lot": 100},
    "valuation": {
        "market_price_max_age_days": 7,
    },
}


def validate_policy(policy: Mapping[str, object] = PORTFOLIO_POLICY) -> None:
    """Fail fast when a code-owned policy threshold is missing or mistyped."""

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
    )
    for path in percentage_paths:
        value = _value_at(policy, path)
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 100:
            raise RuntimeError(f"PORTFOLIO_POLICY.{'.'.join(path)} must be within 0..100")


def _value_at(policy: Mapping[str, object], path: tuple[str, ...]) -> object:
    current: object = policy
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


validate_policy()
