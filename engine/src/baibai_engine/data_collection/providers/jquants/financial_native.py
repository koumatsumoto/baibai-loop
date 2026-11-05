"""Independent financial_summaries L1 contract; no L2 or release dependency."""

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

PREFIX = "l1/jquants/financial-summaries/v1/"
HEAD_KEY = PREFIX + "head.json"
FORMAT = "jquants-financial-native-v1"
MAX_PACK_BYTES = 16 * 1024 * 1024
_CONTRACT = FullDayContract("financial_summaries", PREFIX, FORMAT)
FinancialObjectStore = FullDayObjectStore


class FinancialAttempt(FullDayAttempt):
    source = "financial_summaries"


class FixedFinancialL1(FixedFullDayL1):
    contract = _CONTRACT
    attempt_type = FinancialAttempt


def financial_content_id(request: JQuantsRequest) -> str:
    return full_day_content_id(request, _CONTRACT)


def read_financial_requests(
    store: ObjectStore, *, scopes: dict[str, Any], receipts: dict[str, Any], days: Sequence[date]
) -> Iterator[RetainedRequest]:
    return read_full_day_requests(
        store,
        scopes=scopes,
        receipts=receipts,
        days=days,
        contract=_CONTRACT,
        attempt_type=FinancialAttempt,
    )


class FinancialL1(FullDayL1[FinancialAttempt, FixedFinancialL1]):
    def __init__(self, store: FinancialObjectStore, directory: Path) -> None:
        super().__init__(
            store,
            directory,
            contract=_CONTRACT,
            attempt_type=FinancialAttempt,
            fixed_type=FixedFinancialL1,
            max_pack_bytes=lambda: MAX_PACK_BYTES,
        )
