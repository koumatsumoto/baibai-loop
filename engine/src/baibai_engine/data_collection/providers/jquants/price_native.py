"""Independent daily_bars L1 contract; no L2 or release dependency."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsRequest,
    RetainedRequest,
)
from baibai_engine.data_collection.providers.jquants.full_day_native import (
    FixedFullDayL1,
    FullDayAttempt,
    FullDayContract,
    FullDayL1,
    FullDayObjectStore,
    full_day_content_id,
    read_full_day_requests,
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


def price_content_id(request: JQuantsRequest) -> str:
    return full_day_content_id(request, _CONTRACT)


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
