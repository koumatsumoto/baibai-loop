"""Shared aggregation label for non-selected investment judgments."""

from typing import Literal

type RejectClass = Literal[
    "one_off_earnings",
    "provision_or_writedown",
    "structural_decline",
    "governance_accounting",
    "price_already_converged",
    "supply_demand_liquidity",
    "data_quality",
    "event_wait",
    "other",
]


__all__ = ["RejectClass"]
