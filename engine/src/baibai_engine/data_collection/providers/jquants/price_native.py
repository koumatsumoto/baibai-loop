"""Independent daily_bars L1 contract; no L2 or release dependency."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager
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
    ReadFixedPack,
    VerifiedDayRows,
    full_day_request_packs,
    prefetch_fixed_packs,
    read_full_day_requests,
)
from baibai_engine.storage.objects.r2 import ObjectStore

PREFIX = "l1/jquants/daily-bars/v1/"
HEAD_KEY = PREFIX + "head.json"
FORMAT = "jquants-price-native-v1"
MAX_PACK_BYTES = 16 * 1024 * 1024
# A v1 writer bounds uncompressed JSON; incompressible gzip has a little overhead.
_MAX_COMPRESSED_PACK_BYTES = MAX_PACK_BYTES + 128 * 1024
_CONTRACT = FullDayContract("daily_bars", PREFIX, FORMAT, response_day_key="Date")
PriceObjectStore = FullDayObjectStore


class PriceAttempt(FullDayAttempt):
    source = "daily_bars"


class FixedPriceL1(FixedFullDayL1):
    contract = _CONTRACT
    attempt_type = PriceAttempt


def read_price_requests(
    store: ObjectStore,
    *,
    scopes: dict[str, Any],
    receipts: dict[str, Any],
    days: Sequence[date],
    consume: VerifiedDayRows | None = None,
    read_pack: ReadFixedPack | None = None,
) -> Iterator[RetainedRequest]:
    return read_full_day_requests(
        store,
        scopes=scopes,
        receipts=receipts,
        days=days,
        contract=_CONTRACT,
        attempt_type=PriceAttempt,
        consume=consume,
        read_pack=read_pack,
    )


def prefetch_price_packs(
    store: ObjectStore,
    *,
    scopes: dict[str, Any],
    receipts: dict[str, Any],
    batches: Sequence[Sequence[date]],
    width: int,
) -> AbstractContextManager[ReadFixedPack]:
    digests = [
        digest
        for batch in batches
        for digest in dict.fromkeys(
            pack
            for day in batch
            for _, pack in full_day_request_packs(scopes.get(day.isoformat(), {}), receipts)
        )
    ]
    return prefetch_fixed_packs(
        store, _CONTRACT, digests=digests, width=width, max_bytes=_MAX_COMPRESSED_PACK_BYTES
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
