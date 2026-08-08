"""Quality and provenance labels carried by a derived screening metric.

These values are stored inside EDINET metric rows, so the EDINET extractor's revision
manifest has to contain whatever defines them. They are also part of the candidate row
model, which the extraction never reads. Keeping them in a module of their own lets the
manifest hold the definition without holding the whole candidate model, so a change to
the candidate row no longer invalidates every stored EDINET metric.
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
