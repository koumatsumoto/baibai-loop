"""Code-owned portfolio policy thresholds used by validators."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

type PolicyConfig = Mapping[str, Any]

# 単一プール資本モデル (docs/portfolio-management.md): 投資可能な実資金全体を
# real_capital_yen 1 つで扱い、concentration cap の分母にする。積立に応じて
# 手動で月次更新する簿価。cap は entry 時の sizing 制約で、保有時価の変動では
# 再評価しない (部分トリムをしないため)。
PORTFOLIO_POLICY: dict[str, Any] = {
    "capital_basis": {
        "real_capital_yen": 10_000_000,
    },
    "risk_budget": {
        # ticker は 4-6% 帯で運用し、hard cap は帯の上限 6% (docs は方針、
        # 具体閾値は本 config が正本)。sector も同様に 30-40% 帯の上限 40%。
        "max_ticker_real_concentration_pct": 6.0,
        "max_sector_real_concentration_pct": 40.0,
        "max_playbook_real_concentration_pct": 35.0,
        "max_adv_participation_pct": 5.0,
    },
    "order_constraints": {"board_lot": 100, "price_guard_required": True},
}

_REQUIRED_NUMERIC_PATHS: tuple[tuple[str, ...], ...] = (
    ("capital_basis", "real_capital_yen"),
    ("risk_budget", "max_ticker_real_concentration_pct"),
    ("risk_budget", "max_sector_real_concentration_pct"),
    ("risk_budget", "max_playbook_real_concentration_pct"),
    ("risk_budget", "max_adv_participation_pct"),
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
