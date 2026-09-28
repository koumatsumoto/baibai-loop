"""較正評価で共有するbucket・閾値・出力契約。"""

from __future__ import annotations

from dataclasses import dataclass

# 割安 decile / top-N の「バリュートラップ」判定: 母集団中央値に 20pt 以上劣後。
TRAP_EXCESS_THRESHOLD = -0.20

# 軸評価に要求する最小標本数 (cohort x 軸ごと) 。下回る軸はその cohort で skip。
MIN_AXIS_SAMPLE = 100

# IC 計算に要求する最小標本数。
MIN_IC_SAMPLE = 30

# 1 つの閾値が 1 cohort で除去した行数の下限。閾値ごとに除去数は 2 桁違うので、
# 下限が無いと数行の中央値が数百行の中央値と同じ重みで平均へ入る。
MIN_THRESHOLD_REMOVED_SAMPLE = 20

DECILES = 10

COMPARISON_TOP_NS: tuple[int, ...] = (5, 10, 20)

# gate 条件付き spread の対象 gate (業績悪化 gate。rule_config の deterioration
# threshold と同じ -0.3 を事前固定で用いる) 。
DETERIORATION_THRESHOLD = -0.3

MIN_QUALITY_CONTROL_GROUP = 5

RETURN_CHANGE_CONTROL_FIELDS: tuple[str, ...] = (
    "dividend_yield",
    "per_trailing",
    "pbr",
    "market_cap_oku",
    "avg_turnover_oku",
    "price_change_60d",
)

RETURN_CHANGE_COMPONENT_FIELDS: tuple[str, ...] = (
    "dps_streak_up",
    "dps_guidance_up",
    "dividend_initiation",
)

MARGIN_HYPOTHESIS_AXES: tuple[str, ...] = ("margin_short_to_adv",)

MARGIN_CONTROL_FIELDS: tuple[str, ...] = (
    "market_cap_oku",
    "avg_turnover_oku",
    "per_trailing",
    "dividend_yield",
    "close",
    "price_change_60d",
    "realized_volatility_60d",
    "sector_33",
)

PROFIT_NORMALIZATION_CONTROL_FIELDS: tuple[str, ...] = (
    "per_trailing",
    "pbr",
    "market_cap_oku",
    "sector_33",
)

ASSET_BACKED_THRESHOLD = 0.4

ASSET_BACKED_CONTROL_FIELDS: tuple[str, ...] = (
    "pbr",
    "market_cap_oku",
    "equity_ratio",
)


@dataclass(frozen=True, slots=True, kw_only=True)
class AxisSpec:
    """評価軸。direction=+1 は「高いほど良い(割安)」、-1 は「低いほど良い」。"""

    name: str
    direction: int


AXES: tuple[AxisSpec, ...] = (
    AxisSpec(name="per_forward", direction=-1),
    AxisSpec(name="per_trailing", direction=-1),
    AxisSpec(name="pbr", direction=-1),
    AxisSpec(name="ev_ebitda", direction=-1),
    AxisSpec(name="p_s", direction=-1),
    AxisSpec(name="pcfr", direction=-1),
    AxisSpec(name="ocf_yield", direction=1),
    AxisSpec(name="fcf_yield", direction=1),
    AxisSpec(name="net_cash_to_market_cap", direction=1),
    AxisSpec(name="cash_to_market_cap", direction=1),
    AxisSpec(name="asset_backed_ratio", direction=1),
    AxisSpec(name="equity_ratio", direction=1),
    AxisSpec(name="dividend_yield", direction=1),
    AxisSpec(name="er_annual", direction=1),
    AxisSpec(name="er_reversion_annual", direction=1),
    AxisSpec(name="er_carry_annual", direction=1),
    AxisSpec(name="smg_per_forward", direction=-1),
    AxisSpec(name="smg_per_trailing", direction=-1),
    AxisSpec(name="smg_pbr", direction=-1),
    AxisSpec(name="smg_ev_ebitda", direction=-1),
    AxisSpec(name="smg_p_s", direction=-1),
    AxisSpec(name="srp_per_forward", direction=-1),
    AxisSpec(name="srp_per_trailing", direction=-1),
    AxisSpec(name="srp_pbr", direction=-1),
    AxisSpec(name="srp_ev_ebitda", direction=-1),
    AxisSpec(name="srp_p_s", direction=-1),
    AxisSpec(name="net_share_change_yoy", direction=-1),
    # 同じ株数変化を自己株控除後で測った軸。方向は旧軸と同じ「縮むほど良い」を先に宣言する。
    # 採否は reports/studies/2026-08-17-tradable-share-change/ の事前登録が決める。
    AxisSpec(name="tradable_share_change_yoy", direction=-1),
    AxisSpec(name="accruals_to_assets", direction=-1),
    AxisSpec(name="dps_yoy_latest", direction=1),
    AxisSpec(name="share_count_reduction_streak", direction=1),
    AxisSpec(name="price_change_60d", direction=-1),
    AxisSpec(name="gap_from_52w_low", direction=-1),
    # 需給軸。方向は事前登録として先に宣言する (計測結果を見てから向きを決めない)。
    # margin_long_to_adv / margin_long_share は「買い方が混雑しているほど将来リターンは
    # 劣後する」= 低いほど良い。margin_long_delta_26w は「半年で信用買いが減った
    # 後ほど良い」= 低いほど良い。margin_std_long_share は「期日を持つ overhang が
    # 多いほど劣後する」= 低いほど良い。いずれも計測前の仮説であり、採否は
    # dated report の採用基準で決める。
    AxisSpec(name="margin_long_to_adv", direction=-1),
    AxisSpec(name="margin_short_to_adv", direction=-1),
    AxisSpec(name="margin_long_share", direction=-1),
    AxisSpec(name="margin_long_delta_26w", direction=-1),
    AxisSpec(name="margin_std_long_share", direction=-1),
    AxisSpec(name="normalized_per_3fy", direction=-1),
    AxisSpec(name="normalized_per_5fy", direction=-1),
)

# gate 条件付き評価を行う「割安軸」 (この軸の best decile 内で gate を比較する) 。
GATE_BASE_AXES: tuple[str, ...] = ("per_trailing", "pbr", "ocf_yield")

# 収束実現 (implied upside → realized) を測る sector 相対 gap 軸。
REVERSION_AXES: tuple[str, ...] = ("smg_per_trailing", "smg_pbr", "smg_ev_ebitda")

# 業種中央値との差を測る軸。`PanelRow.smg_market_fallback` はこの prefix を外した
# metric 名を並べるので、軸と素性はその語彙で対応する。
_SECTOR_MEDIAN_AXIS_PREFIX = "smg_"

SECTOR_MEDIAN_AXES: tuple[str, ...] = (
    "smg_per_forward",
    "smg_per_trailing",
    "smg_pbr",
    "smg_ev_ebitda",
    "smg_p_s",
)
