"""EDINET抽出値と企業価値の機械計算で共有する品質・出所の語彙。

抽出は旧Screeningの出力modelへ依存しない。保存値の意味を変える場合は
edinet_metrics/revision.pyの明示的な抽出意味revisionを更新する。
"""

from __future__ import annotations

from enum import StrEnum


class TTMQuality(StrEnum):
    EXACT = "exact"
    APPROXIMATED = "approximated"
    UNAVAILABLE = "unavailable"


class OperatingProfitSource(StrEnum):
    OPERATING_PROFIT = "OperatingProfit"
    ORDINARY_PROFIT = "OrdinaryProfit"
    PROFIT = "Profit"
    NULL = "null"
