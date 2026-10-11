"""Freeze the saved common-stock universe; never acquire a newer master."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from baibai_engine.market.master_snapshot import MIN_COMMON_STOCK_MASTER_ROWS
from baibai_engine.market.sqlite.lake_origin import read_lake_store_origin
from baibai_engine.market.sqlite.reader import read_eq_master_asof
from baibai_engine.market.ticker import normalize_ticker
from baibai_engine.market.universe import (
    ELIGIBLE_MARKETS,
    NON_COMMON_STOCK_SECTORS,
    TSE_33_SECTORS,
    UniverseSourceDriftError,
)


def frozen_universe(database: Path, master_asof: date) -> tuple[tuple[str, ...], str]:
    """Require an exact saved master and a hydrated fixed-release origin."""
    if master_asof > datetime.now(ZoneInfo("Asia/Tokyo")).date():
        raise ValueError("retail saved master cannot be in the future")
    snapshot = read_eq_master_asof(database, master_asof)
    origin = read_lake_store_origin(database)
    if (
        snapshot.status != "exact_date"
        or len(snapshot.masters) < MIN_COMMON_STOCK_MASTER_ROWS
        or origin is None
    ):
        raise ValueError("retail universe requires a complete exact saved master and L1 origin")
    selected = []
    for security in snapshot.masters:
        if security.market_segment.upper() not in ELIGIBLE_MARKETS:
            continue
        if security.sector_33 not in TSE_33_SECTORS | NON_COMMON_STOCK_SECTORS:
            raise UniverseSourceDriftError("Unknown J-Quants sector classification")
        if security.sector_33 in TSE_33_SECTORS:
            ticker = normalize_ticker(security.ticker)
            if re.fullmatch(r"[0-9][0-9A-Z]{3}", ticker) is None:
                raise ValueError("invalid TSE common-stock ticker")
            selected.append(ticker)
    tickers = tuple(sorted(selected))
    if len(tickers) < MIN_COMMON_STOCK_MASTER_ROWS or len(set(tickers)) != len(tickers):
        raise ValueError("retail universe requires a complete distinct common-stock population")
    binding = json.dumps(
        {
            "master_asof": master_asof.isoformat(),
            "release_id": origin.release_id,
            "release_manifest_sha256": origin.release_manifest_sha256,
            "tickers_sha256": hashlib.sha256("\n".join(tickers).encode()).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return tickers, binding
