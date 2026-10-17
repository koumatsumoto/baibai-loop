"""Received public equity page bytes, independent of parsing and derived values."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

type Source = Literal["monex", "rakuten", "kabumap"]
type Role = Literal["page", "iframe"]


@dataclass(frozen=True, slots=True)
class EquityWebSourceResponse:
    receipt_id: str
    attempt_id: str
    source: Source
    requested_ticker: str
    role: Role
    parent_receipt_id: str | None
    requested_url: str
    received_at_utc: datetime
    selected_encoding: str
    body: bytes = field(repr=False)
