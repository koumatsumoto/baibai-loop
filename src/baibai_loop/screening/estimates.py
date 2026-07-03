"""機械期待値 E[r]: 成分分解付きの年率リターン見積り (単一の合成スコアではない)。

doctrine 柱 5(b) との整合: E[r] は単位 (%/年) と前提 (anchor・実現率・cap) を持つ
「見積り」であり、出力は必ず reversion / carry の成分と入力を併記する。採否判断は
人間に残り、ランキングへの反映は #295 の事前登録検証を通過した場合のみ行う。

式 (較正リプレイの実測に基づく形):

- anchor 倍率 = min(sector 中央値倍率, 自己レンジ中央値倍率) — 保守側 (低い方) を
  採る。片方欠損時はもう片方。
- implied upside = anchor / current - 1 (signed。割高なら負)
- reversion (年率) = REALIZATION_RATE_ANNUAL x clip(upside, ±UPSIDE_CAP)
  — baseline 計測 (reports/2026-07-03-estimate-calibration-baseline.md §5) で
  収束実現は implied upside に単調でなく、~50% を超える deep discount は gap の
  1% 弱/6m しか実現しなかったため、上側を cap して線形近似する。
- carry (年率) = 実績配当利回り + clip(自社株買い利回り, ±BUYBACK_CLIP)
  — 自社株買い利回り = -net_share_change_yoy (株数縮小 = 正)。
- E[r] (年率) = reversion + carry
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean

from .schema import DerivedMetrics, FinancialSnapshot

# 較正パラメータ。値の出典は較正リプレイの収束実現テーブル (直近の較正レポート) で、
# 更新するときは新しい計測とセットで変更する (grid search はしない。自由パラメータは
# この 3 つに限定する)。
REALIZATION_RATE_ANNUAL = 0.10
UPSIDE_CAP = 0.50
BUYBACK_CLIP = 0.05

# anchor に使う倍率軸。資産 (pbr) と収益 (per_forward → per_trailing fallback) の
# 2 系統を blend する (baseline で予測力上位の 2 軸。単一軸のノイズを平均で薄める)。
_EARNINGS_METRICS = ("per_forward", "per_trailing")


@dataclass(frozen=True, slots=True, kw_only=True)
class ExpectedReturnEstimate:
    """成分分解付きの E[r]。すべて年率の比率 (0.1 = 10%/年)。"""

    er_annual: float
    reversion_annual: float
    carry_annual: float
    upside_blend: float
    upside_capped: float
    anchor_metrics: str
    dividend_yield: float | None
    buyback_yield: float | None
    fv_sector_median_yen: float | None
    fv_self_range_yen: float | None


def estimate_expected_return(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    *,
    close: float | None,
) -> ExpectedReturnEstimate | None:
    """E[r] と FV アンカーを見積もる。anchor 倍率が 1 軸も取れなければ None。"""
    upsides: dict[str, float] = {}
    sector_ratios: list[float] = []
    self_ratios: list[float] = []

    for metric in _anchor_metrics(financial):
        current = _positive(getattr(financial, metric))
        if current is None:
            continue
        sector_median = _positive(derived.sector_median_value.get(metric))
        self_median = _positive(derived.self_range_median.get(metric))
        anchors = [value for value in (sector_median, self_median) if value is not None]
        if not anchors:
            continue
        conservative_anchor = min(anchors)
        upsides[metric] = conservative_anchor / current - 1
        if sector_median is not None:
            sector_ratios.append(sector_median / current)
        if self_median is not None:
            self_ratios.append(self_median / current)

    if not upsides:
        return None

    upside_blend = fmean(upsides.values())
    upside_capped = _clip(upside_blend, UPSIDE_CAP)
    reversion_annual = REALIZATION_RATE_ANNUAL * upside_capped

    dividend_yield = financial.dividend_yield
    buyback_yield = (
        -financial.net_share_change_yoy if financial.net_share_change_yoy is not None else None
    )
    carry_annual = (dividend_yield or 0.0) + _clip(buyback_yield or 0.0, BUYBACK_CLIP)

    return ExpectedReturnEstimate(
        er_annual=reversion_annual + carry_annual,
        reversion_annual=reversion_annual,
        carry_annual=carry_annual,
        upside_blend=upside_blend,
        upside_capped=upside_capped,
        anchor_metrics=",".join(sorted(upsides)),
        dividend_yield=dividend_yield,
        buyback_yield=buyback_yield,
        fv_sector_median_yen=(
            close * fmean(sector_ratios) if close is not None and sector_ratios else None
        ),
        fv_self_range_yen=(
            close * fmean(self_ratios) if close is not None and self_ratios else None
        ),
    )


def _anchor_metrics(financial: FinancialSnapshot) -> tuple[str, ...]:
    """資産 anchor (pbr) + 収益 anchor (forward 優先、無ければ trailing)。"""
    earnings = next(
        (metric for metric in _EARNINGS_METRICS if _positive(getattr(financial, metric))),
        None,
    )
    return ("pbr", earnings) if earnings else ("pbr",)


def _positive(value: float | None) -> float | None:
    return value if value is not None and value > 0 else None


def _clip(value: float, bound: float) -> float:
    return max(-bound, min(bound, value))
