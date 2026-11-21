"""Code-owned portfolio policy thresholds used by validators."""

from __future__ import annotations

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
