"""機械期待値 E[r]: 成分分解付きの年率リターン見積り (単一の合成スコアではない)。

doctrine 柱 5(b) との整合: E[r] は単位 (%/年) と前提 (anchor・実現率・cap) を持つ
「見積り」であり、出力は必ず reversion / carry の成分と入力を併記する。採否判断は
人間に残り、ランキングへの反映は #295 の事前登録検証を通過した場合のみ行う。

式 (較正リプレイの実測に基づく形):

- anchor 倍率 = min(sector 中央値倍率, 自己レンジ中央値倍率) — 保守側 (低い方) を
  採る。片方欠損時はもう片方。
  — 自己レンジ側は**倍率の履歴ではなく価格の履歴**である。`_valuation_history` が
    fundamentals を最新値で固定して調整後終値だけを動かすので、価格比例の軸
    (per_forward / per_trailing / pbr / p_s) では `自己中央値 / 現値` が軸によらず
    `median(750 営業日終値) / 現値` に一致する (実データ 3,424 銘柄で 100% 一致)。
    自己レンジ側が binding する銘柄では、この成分は倍率でなく価格の平均回帰を測る。
- implied upside = anchor / current - 1 (signed。割高なら負)
- reversion (年率) = REALIZATION_RATE_ANNUAL x clip(upside, ±UPSIDE_CAP)
  — model policy parameter により過大な upside を保守側へ制限する。
- carry (年率) = 配当利回り + clip(自社株買い利回り, ±BUYBACK_CLIP)
  — 配当利回りは carry 用に解決した将来利回り (予想 DPS を最優先、無ければ
    accrual 期間の分割 factor で調整した実績 DPS。分割前配当と分割後株価の混在で
    利回りが膨らむのを防ぐ。詳細は metrics._resolve_dividend_carry)。
  — 株数縮小利回り = -net_share_change_yoy (株数縮小 = 正)。これは自己株式を含む
    グロス発行済株式数の前年比であり、自社株買いそのものではない。日本の自社株買いは
    取得した株式を自己株式へ入れるだけで、発行済株式総数は消却するまで減らない
    (EDINET 自己株券買付状況報告書との突合で、実買付のあった 556 期のうちこの量が
    捉えるのは 36.3%)。
- E[r] (年率) = reversion + carry
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from statistics import fmean
from typing import Literal

from .schema import UNRESOLVED_DIVIDEND_BASIS, DerivedMetrics, FinancialSnapshot

# 現在の MODEL_V1 policy parameters。実証的な変更は long-horizon authority を満たす
# artifact と人間レビューを経て code で明示的に変更し、自動更新はしない。
REALIZATION_RATE_ANNUAL = 0.10
UPSIDE_CAP = 0.50
BUYBACK_CLIP = 0.05
EXPECTED_RETURN_MODEL_VERSION = "expected-return-v1"
EXPECTED_RETURN_UNIT = "annual_ratio"

# anchor に使う倍率軸。資産 (pbr) と収益 (per_forward → per_trailing fallback) の
# 2 系統を blend する。ただし両軸とも自己レンジ側で binding した銘柄では 2 つの upside が
# 同じ値になり (自己レンジは価格の履歴なので軸に依存しない)、平均はノイズを薄めない。
# as-of 2026-03-31 の実測では、E[r] を持つ 3,773 銘柄のうち 1,434 (38.0%) がこの形。
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
    origin: Literal["estimate"] = "estimate"
    model_version: str = EXPECTED_RETURN_MODEL_VERSION
    unit: str = EXPECTED_RETURN_UNIT
    assumptions: str = (
        "reversion=0.10*clip(implied_upside,+/-0.50); "
        "carry=dividend_yield(forecast_preferred,split_safe)+clip(buyback_yield,+/-0.05)"
    )


def estimate_expected_return(
    financial: FinancialSnapshot,
    derived: DerivedMetrics,
    *,
    close: float | None,
) -> ExpectedReturnEstimate | None:
    """E[r] と FV アンカーを見積もる。anchor 倍率が 1 軸も取れなければ None。

    配当の株式基準が確定できない行も None にする。carry は `dividend_yield or 0.0` で
    組むので、利回りを出さないことが下流では「無配」の主張になり、実際に配当を払って
    いる銘柄を E[r] 降順から一方向に落とす。値を知らないことと 0 であることは別なので、
    知らない年度は順位を付けない。
    """
    if financial.dividend_basis == UNRESOLVED_DIVIDEND_BASIS:
        return None

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
    if not isfinite(upside_blend):
        return None
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
