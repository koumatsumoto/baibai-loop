"""EDINET抽出値と企業財務で共有する測定期間の品質。

抽出は旧Screeningの出力modelへ依存しない。保存値の意味を変える場合は
edinet_metrics/revision.pyの明示的な抽出意味revisionを更新する。
"""

from __future__ import annotations

from enum import StrEnum


class TTMQuality(StrEnum):
    EXACT = "exact"
    APPROXIMATED = "approximated"
    UNAVAILABLE = "unavailable"
