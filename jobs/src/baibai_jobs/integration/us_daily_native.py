"""Commit the existing validated US daily capture to native L1, not Market SQLite."""

from datetime import UTC
from hashlib import sha256
from uuid import UUID

from baibai_engine.data_collection.providers.alpaca_sip_native import (
    AlpacaSIPDay,
    AlpacaSIPDayBar,
    SavedAlpacaSIPDay,
    save_alpaca_sip_day,
)
from baibai_engine.storage.json import canonical_json
from baibai_engine.storage.objects.r2 import ObjectStore
from baibai_jobs.integration.us_daily_prices import USDailyPriceCollection

_INSTRUMENT_PREFIX = "us:alpaca:"


def write_alpaca_sip_day(
    store: ObjectStore,
    collection: USDailyPriceCollection,
) -> SavedAlpacaSIPDay | None:
    plan = collection.plan
    by_symbol: dict[str, UUID] = {}
    scope: list[tuple[str, str, str]] = []
    for target in plan.targets:
        mic, symbol = target.listing_key
        identity = target.instrument_id
        if not identity.startswith(_INSTRUMENT_PREFIX):
            raise ValueError("native SIP history requires Alpaca asset UUIDs")
        asset = UUID(identity.removeprefix(_INSTRUMENT_PREFIX))
        by_symbol[symbol] = asset
        scope.append((str(asset), mic, symbol))
    bars = []
    for target, row in collection.observations:
        mic, symbol = target.listing_key
        if row.feed != "sip" or row.adjustment != "raw" or row.trade_date != plan.price_session:
            raise ValueError("native history requires the target session's raw SIP bars")
        source = max(row.sources, key=lambda receipt: receipt.received_at)
        bars.append(
            AlpacaSIPDayBar(
                asset_id=by_symbol[symbol],
                mic=mic,
                symbol=symbol,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=row.volume,
                received_at=source.received_at.astimezone(UTC),
                receipt_sha256=source.body_sha256,
            )
        )
    day = AlpacaSIPDay(
        session=plan.price_session,
        closed_at=collection.session_close_at.astimezone(UTC),
        scope_sha256=sha256(canonical_json(sorted(scope))).hexdigest(),
        bars=tuple(sorted(bars, key=lambda bar: bar.asset_id)),
        missing=tuple(
            sorted(
                by_symbol[symbol]
                for item in collection.receipts
                for symbol, _ in item.coverage.missing
            )
        ),
        unverified=tuple(
            sorted(
                by_symbol[symbol]
                for item in collection.receipts
                for symbol, _ in item.coverage.unverified
            )
        ),
    )
    return save_alpaca_sip_day(store, day)
