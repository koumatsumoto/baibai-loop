"""Code-owned portfolio policy thresholds used by validators."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

type PolicyConfig = Mapping[str, Any]

# 資本額は ledger event から再計算する。ここには状態を置かず、判断時に適用する
# warning line と注文制約だけを置く。
PORTFOLIO_POLICY: dict[str, Any] = {
    "cash_management": {
        "monthly_contribution_yen": 400_000,
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
    "valuation": {"market_price_max_age_days": 7},
    # 要求利回りに届かない境界帯へ、縮小 lot と bucket 上限つきで入るための枠。
    # 機械 E[r] 上位群は 3y/5y の全 cohort で母集団を上回る一方、正規化と据え置き倍率を
    # 積んだ research の base は要求 8.5% に届かず全件棄却になっていた。その乖離を
    # 観測ゼロのままにしないための bounded な経路であり、永久損失 7 軸・独立レビュー・
    # human override は一切緩めない。計測は reports/2026-08-06-bargain-capture-diagnosis.md。
    "starter_band": {
        "required_return_floor_pct": 7.0,
        "required_return_ceiling_pct": 8.5,
        "max_order_notional_yen": 100_000,
        "max_bucket_pct": 10.0,
    },
}

_REQUIRED_NUMERIC_PATHS: tuple[tuple[str, ...], ...] = (
    ("cash_management", "monthly_contribution_yen"),
    ("cash_management", "dry_powder_warning_pct"),
    ("cash_management", "override_max_days"),
    ("risk_budget", "max_ticker_concentration_pct"),
    ("risk_budget", "max_sector_concentration_pct"),
    ("risk_budget", "max_common_factor_concentration_pct"),
    ("risk_budget", "max_adv_participation_pct"),
    ("order_constraints", "board_lot"),
    ("valuation", "market_price_max_age_days"),
    ("starter_band", "required_return_floor_pct"),
    ("starter_band", "required_return_ceiling_pct"),
    ("starter_band", "max_order_notional_yen"),
    ("starter_band", "max_bucket_pct"),
)


def validate_policy(policy: PolicyConfig = PORTFOLIO_POLICY) -> None:
    """Fail fast when a code-owned policy threshold is missing or mistyped."""

    for path in _REQUIRED_NUMERIC_PATHS:
        value = _value_at(policy, path)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            dotted = ".".join(path)
            raise RuntimeError(f"PORTFOLIO_POLICY.{dotted} must be numeric")
    positive_integer_paths = (
        ("cash_management", "monthly_contribution_yen"),
        ("cash_management", "override_max_days"),
        ("order_constraints", "board_lot"),
        ("valuation", "market_price_max_age_days"),
        ("starter_band", "max_order_notional_yen"),
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
        ("starter_band", "required_return_floor_pct"),
        ("starter_band", "required_return_ceiling_pct"),
        ("starter_band", "max_bucket_pct"),
    )
    for path in percentage_paths:
        value = _value_at(policy, path)
        if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 100:
            raise RuntimeError(f"PORTFOLIO_POLICY.{'.'.join(path)} must be within 0..100")
    # 帯が空 (floor >= ceiling) なら starter は宣言できても常に拒否される。要求利回りを
    # 下げる緩和で唯一有界性を担保するのが bucket 上限なので、桁ミスをここで止める。
    floor = _value_at(policy, ("starter_band", "required_return_floor_pct"))
    ceiling = _value_at(policy, ("starter_band", "required_return_ceiling_pct"))
    if isinstance(floor, int | float) and isinstance(ceiling, int | float) and floor >= ceiling:
        raise RuntimeError(
            "PORTFOLIO_POLICY.starter_band.required_return_floor_pct must be below the ceiling"
        )
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
