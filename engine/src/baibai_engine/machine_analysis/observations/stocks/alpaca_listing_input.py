"""Use one complete Alpaca active inventory as current US research identity."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from baibai_engine.data_collection.providers.alpaca import is_alpaca_symbol
from baibai_engine.data_collection.providers.alpaca_assets import AlpacaAssetsReceipt
from baibai_engine.data_collection.providers.nasdaq_symbols import PrimaryUSVenue
from baibai_engine.machine_analysis.observations.stocks.listing_master import (
    ListingMasterRow,
    ListingSlice,
)
from baibai_engine.machine_analysis.observations.stocks.listing_master_file import (
    ALPACA_ASSETS_SOURCE,
    CurrentListingMarket,
)

_NY = ZoneInfo("America/New_York")
_SUPPORTED_EXCHANGES: dict[str, PrimaryUSVenue] = {
    "NASDAQ": "XNAS",
    "NYSE": "XNYS",
    "AMEX": "XASE",
    "ARCA": "ARCX",
    "NYSEARCA": "ARCX",
    "BATS": "BATS",
}


@dataclass(frozen=True, slots=True)
class AlpacaResearchInventory:
    current: CurrentListingMarket
    observed_by_exchange: dict[str, int]
    excluded: dict[str, int]


def prepare_alpaca_research_inventory(
    receipt: AlpacaAssetsReceipt, *, previous: CurrentListingMarket | None = None
) -> AlpacaResearchInventory:
    """Keep native identity; do not infer product type, SIP success or order eligibility."""
    instant = receipt.received_at
    day = instant.astimezone(_NY).date()
    prior = {}
    if previous is not None and previous.source == ALPACA_ASSETS_SOURCE:
        if instant < previous.received_at:
            raise ValueError("Alpaca inventory receipt is older than the current scope")
        if instant == previous.received_at and receipt.body_sha256 != previous.alpaca_assets_sha256:
            raise ValueError("different Alpaca assets claim the same receipt clock")
        prior = {row.key: row for row in previous.slice.rows}

    by_exchange = Counter(row.exchange for row in receipt.assets)
    symbols = Counter(row.symbol for row in receipt.assets)
    excluded: Counter[str] = Counter()
    rows: list[ListingMasterRow] = []
    for asset in receipt.assets:
        venue = _SUPPORTED_EXCHANGES.get(asset.exchange)
        if asset.exchange == "OTC":
            excluded["other_feed"] += 1
        elif venue is None:
            excluded["unsupported_venue"] += 1
        elif not is_alpaca_symbol(asset.symbol):
            excluded["unmapped_symbol"] += 1
        elif symbols[asset.symbol] != 1:
            # A SIP request carries only the symbol, not the exchange or UUID.
            excluded["ambiguous_symbol"] += 1
        else:
            key = (venue, asset.symbol)
            instrument_id = f"us:alpaca:{asset.id}"
            existing = prior.get(key)
            observed_at = (
                existing.identity_observed_at
                if existing is not None and existing.instrument_id == instrument_id
                else instant
            )
            assert observed_at is not None  # Validated by CurrentListingMarket.
            rows.append(
                ListingMasterRow(
                    key=key,
                    security_name=asset.name,
                    issue_type=None,  # us_equity covers products other than common stock.
                    listing_state="listed",
                    instrument_id=instrument_id,
                    identity_since=observed_at.astimezone(_NY).date(),
                    identity_observed_at=observed_at,
                )
            )
    return AlpacaResearchInventory(
        current=CurrentListingMarket(
            slice=ListingSlice("US", day, tuple(sorted(rows, key=lambda row: row.key))),
            source=ALPACA_ASSETS_SOURCE,
            received_at=instant,
            received_count=len(receipt.assets),
            alpaca_assets_sha256=receipt.body_sha256,
        ),
        observed_by_exchange=dict(by_exchange),
        excluded=dict(excluded),
    )
