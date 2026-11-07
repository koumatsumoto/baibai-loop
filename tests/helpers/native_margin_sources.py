"""Synthetic retained margin originals for unrelated integration boundaries."""

import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from baibai_engine.data_collection.providers.jquants.margin_alerts_native import MarginAlertsL1
from baibai_engine.data_collection.providers.jquants.margin_interest_native import MarginInterestL1
from baibai_engine.data_collection.providers.jquants.short_sale_reports_native import (
    HEAD_KEY,
    ShortSaleReportsL1,
)
from tests.helpers.short_sale_reports_native import attempt


def initialize_margin_sources(store, directory: Path) -> None:
    for name, owner in (
        ("margin-alerts", MarginAlertsL1),
        ("margin-interest", MarginInterestL1),
        ("short-sales", ShortSaleReportsL1),
    ):
        owner(store, directory / name).initialize()


def seed_short_reports(store, market: Path, directory: Path) -> None:
    """Give a generic publication fixture matching synthetic native originals once."""
    if store.head(HEAD_KEY) is not None:
        return
    owner = ShortSaleReportsL1(store, directory)
    owner.initialize()
    with closing(sqlite3.connect(market)) as connection:
        rows = connection.execute(
            "SELECT disclosed_at,calculated_at,ticker,short_seller_name,"
            "discretionary_investment_contractor_name,investment_fund_name,"
            "short_ratio,short_shares,short_trading_units FROM jquants_short_sale_reports "
            "ORDER BY disclosed_at,source_ordinal"
        ).fetchall()
        coverage = connection.execute(
            "SELECT fetched_at_utc FROM source_coverage WHERE source='jquants_short_sale_reports'"
        ).fetchone()
    grouped = {}
    for disclosed, calculated, ticker, seller, contractor, fund, ratio, shares, units in rows:
        grouped.setdefault(disclosed, []).append(
            {
                "DiscDate": disclosed,
                "CalcDate": calculated,
                "Code": ticker + "0",
                "SSName": seller,
                "DICName": contractor,
                "FundName": fund,
                "ShrtPosToSO": ratio,
                "ShrtPosInShs": shares,
                "ShrtPosInTrdUnits": units,
            }
        )
    at = datetime.fromisoformat(coverage[0])
    owner.commit(
        [
            attempt(f"synthetic-short-{day}", day=date.fromisoformat(day), at=at, rows=records)
            for day, records in grouped.items()
        ]
    )
