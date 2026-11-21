"""Freeze the saved common-stock universe; never acquire a newer master."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.jquants.contracts import JQuantsRequest
from baibai_engine.data_collection.providers.jquants.master_native import is_master_for_day
from baibai_engine.machine_analysis.observations.stocks.master_snapshot import (
    MIN_COMMON_STOCK_MASTER_ROWS,
    masters_from_payloads,
    validate_master_snapshot,
)
from baibai_engine.machine_analysis.observations.stocks.models import SecurityMaster
from baibai_engine.machine_analysis.observations.stocks.universe import ELIGIBLE_MARKETS


def received_master_universe(request: JQuantsRequest, master_asof: date) -> tuple[str, ...]:
    """Use an exact complete native receipt without assigning a full lake origin."""
    _require_master_date(master_asof)
    if not is_master_for_day(request, master_asof):
        raise ValueError("external assessment requires an exact-date master receipt")
    snapshot = validate_master_snapshot(request.records(), master_asof)
    return _common_stock_tickers(masters_from_payloads([row[2] for row in snapshot.rows]))


def _require_master_date(master_asof: date) -> None:
    if master_asof > datetime.now(ZoneInfo("Asia/Tokyo")).date():
        raise ValueError("external assessment saved master cannot be in the future")


def _common_stock_tickers(masters: Sequence[SecurityMaster]) -> tuple[str, ...]:
    tickers = tuple(
        sorted(
            security.ticker
            for security in masters
            if security.is_common_stock and security.market_segment.upper() in ELIGIBLE_MARKETS
        )
    )
    if any(re.fullmatch(r"[0-9][0-9A-Z]{3}", ticker) is None for ticker in tickers):
        raise ValueError("invalid TSE common-stock ticker")
    if len(tickers) < MIN_COMMON_STOCK_MASTER_ROWS or len(set(tickers)) != len(tickers):
        raise ValueError(
            "external assessment universe requires a complete distinct common-stock population"
        )
    return tickers
