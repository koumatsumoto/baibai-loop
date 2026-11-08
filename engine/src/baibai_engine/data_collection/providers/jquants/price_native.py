"""Independent daily_bars L1 contract; no L2 or release dependency."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.contracts import RetainedRequest
from baibai_engine.data_collection.providers.jquants.full_day_native import (
    FixedFullDayL1,
    FullDayAttempt,
    FullDayContract,
    FullDayL1,
    FullDayObjectStore,
    read_full_day_requests,
    verify_migrated_full_day_receipts,
)
from baibai_engine.storage.objects.r2 import ObjectStore

PREFIX = "l1/jquants/daily-bars/v1/"
HEAD_KEY = PREFIX + "head.json"
FORMAT = "jquants-price-native-v1"
MAX_PACK_BYTES = 16 * 1024 * 1024
_CONTRACT = FullDayContract("daily_bars", PREFIX, FORMAT)
PriceObjectStore = FullDayObjectStore


class PriceAttempt(FullDayAttempt):
    source = "daily_bars"


class FixedPriceL1(FixedFullDayL1):
    contract = _CONTRACT
    attempt_type = PriceAttempt


def read_price_requests(
    store: ObjectStore, *, scopes: dict[str, Any], receipts: dict[str, Any], days: Sequence[date]
) -> Iterator[RetainedRequest]:
    return read_full_day_requests(
        store,
        scopes=scopes,
        receipts=receipts,
        days=days,
        contract=_CONTRACT,
        attempt_type=PriceAttempt,
    )


class PriceL1(FullDayL1[PriceAttempt, FixedPriceL1]):
    def __init__(self, store: PriceObjectStore, directory: Path) -> None:
        super().__init__(
            store,
            directory,
            contract=_CONTRACT,
            attempt_type=PriceAttempt,
            fixed_type=FixedPriceL1,
            max_pack_bytes=lambda: MAX_PACK_BYTES,
        )


def verify_migrated_price_receipts(
    connection: sqlite3.Connection, request_ids: Sequence[str], fingerprints: Mapping[str, str]
) -> set[str]:
    """Accept legacy price bytes only when the finite cutover already retained them."""
    return verify_migrated_full_day_receipts(
        connection, request_ids, fingerprints, attempt_type=PriceAttempt
    )
