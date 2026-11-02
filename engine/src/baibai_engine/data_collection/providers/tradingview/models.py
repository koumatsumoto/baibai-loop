"""Immutable context and SDK-result representation for one received tool call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class TradingViewSourceReceipt:
    receipt_id: str
    # Acquisition cycle key; the actual receipt instant remains received_at_utc.
    snapshot_date: date
    master_asof: date
    tool_name: str
    received_at_utc: datetime
    requested_symbols: tuple[str, ...]
    requested_columns: tuple[str, ...]
    result_json: str
