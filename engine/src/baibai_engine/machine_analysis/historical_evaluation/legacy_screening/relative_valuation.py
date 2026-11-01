"""旧較正の比較母集団に対する倍率・自己レンジ評価。現行候補政策ではない。"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import median

from baibai_engine.machine_analysis.historical_evaluation.legacy_screening.valuation_history import (
    self_range_position,
)
from baibai_engine.machine_analysis.models.company_financials.metrics.models import (
    FinancialSnapshot,
)

VALUATION_METRICS = ("per_forward", "per_trailing", "pbr", "ev_ebitda", "p_s")
# 少数の同業標本へ依存しすぎない計算上の慣例。経済的最適値ではない (#12, #1153)。
# docs/reference/estimate-calibration.md
MIN_SECTOR_MEDIAN_POPULATION = 10
SECTOR_MEDIAN_BASIS_SECTOR = "sector"
SECTOR_MEDIAN_BASIS_MARKET = "market"


@dataclass(frozen=True)
class RelativeValuation:
    sector_median_gap: Mapping[str, float | None]
    sector_median_value: Mapping[str, float | None]
    sector_median_basis: Mapping[str, str]
    self_range_position: Mapping[str, float | None]
    self_range_median: Mapping[str, float | None]


def relative_valuation(
    snapshot: FinancialSnapshot,
    sector_metric_values: Mapping[str, Sequence[float]],
    market_metric_values: Mapping[str, Sequence[float]],
    valuation_history: Mapping[str, Sequence[float]],
) -> RelativeValuation:
    sector_gaps: dict[str, float | None] = {}
    sector_medians: dict[str, float | None] = {}
    sector_bases: dict[str, str] = {}
    self_positions: dict[str, float | None] = {}
    self_medians: dict[str, float | None] = {}
    for metric in VALUATION_METRICS:
        current = getattr(snapshot, metric)
        sector_values = sector_metric_values.get(metric, [])
        on_sector = len(sector_values) >= MIN_SECTOR_MEDIAN_POPULATION
        baseline = sector_values if on_sector else market_metric_values.get(metric, [])
        sector_median = median(baseline) if baseline else None
        sector_medians[metric] = sector_median
        # どちらの母集団が答えたかを値と同じ粒度で残す。両者は同じ語で呼ばれるが
        # 別の量である。市場fallbackには業種構成の違いも入るため、素性が無いと
        # 同業比較なのか市場比較なのかを後から区別できない。
        # 中央値そのものが出なかった軸には基準が無い。どちらも答えていないのに
        # 「市場へ落ちた」と書くと、EDINET 由来の軸のように母集団全体で値が立たない
        # 軸が全行 fallback として並び、実際に落ちた軸と見分けが付かなくなる。
        if sector_median is not None:
            sector_bases[metric] = (
                SECTOR_MEDIAN_BASIS_SECTOR if on_sector else SECTOR_MEDIAN_BASIS_MARKET
            )
        sector_gaps[metric] = (
            ((current / sector_median) - 1.0)
            if current is not None and sector_median not in (None, 0)
            else None
        )
        history_values = valuation_history.get(metric, [])
        self_positions[metric] = self_range_position(history_values, current)
        # 自己レンジの中央値。機械 E[r] の保守側 anchor に使う。標本が薄い履歴
        # (直近上場等) の中央値は anchor として不安定なため 100 本を下限にする。
        # `valuation_history` は fundamentals を最新値で固定して価格だけを動かすので、
        # これは倍率の履歴ではなく価格の履歴を倍率の単位で表したものである。価格比例の
        # 軸では `自己中央値 / 現値` が軸によらず `median(終値) / 現在株価` に一致する。
        self_medians[metric] = median(history_values) if len(history_values) >= 100 else None

    return RelativeValuation(
        sector_median_gap=sector_gaps,
        sector_median_value=sector_medians,
        sector_median_basis=sector_bases,
        self_range_position=self_positions,
        self_range_median=self_medians,
    )


def normalized_earnings_sector_gaps(
    observations: Sequence[tuple[str, str, float | None]],
) -> dict[str, float | None]:
    """値で渡された正の正規化PERを、同じ選択母集団内の倍率中央値と比較する。"""
    by_sector: dict[str, list[float]] = defaultdict(list)
    market: list[float] = []
    for _ticker, sector, value in observations:
        if value is not None:
            market.append(value)
            by_sector[sector].append(value)
    market_center = median(market) if market else None
    centers = {
        sector: median(values) if len(values) >= MIN_SECTOR_MEDIAN_POPULATION else market_center
        for sector, values in by_sector.items()
    }
    output: dict[str, float | None] = {}
    for ticker, sector, value in observations:
        center = centers.get(sector, market_center)
        output[ticker] = (
            value / center - 1 if value is not None and center not in (None, 0) else None
        )
    return output
