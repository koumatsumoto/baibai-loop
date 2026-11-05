"""Independent disclosure-day originals for investor-level short-position reports."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import date, timedelta
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
)
from baibai_engine.data_collection.providers.jquants.market_rules import (
    MARKET_REVISION_OVERLAP_DAYS,
)
from baibai_engine.storage.objects.r2 import ObjectStore

PREFIX = "l1/jquants/short-sale-reports/v1/"
HEAD_KEY = PREFIX + "head.json"
FORMAT = "jquants-short-sale-reports-native-v1"
_CONTRACT = FullDayContract(
    "short_sale_reports",
    PREFIX,
    FORMAT,
    query_date_format="%Y-%m-%d",
    query_date_key="disc_date",
)


class ShortSaleReportsAttempt(FullDayAttempt):
    source = "short_sale_reports"


class FixedShortSaleReportsL1(FixedFullDayL1):
    contract = _CONTRACT
    attempt_type = ShortSaleReportsAttempt


class ShortSaleReportsL1(FullDayL1[ShortSaleReportsAttempt, FixedShortSaleReportsL1]):
    def __init__(self, store: FullDayObjectStore, directory: Path) -> None:
        super().__init__(
            store,
            directory,
            contract=_CONTRACT,
            attempt_type=ShortSaleReportsAttempt,
            fixed_type=FixedShortSaleReportsL1,
            max_pack_bytes=lambda: 16 * 1024 * 1024,
        )


def plan_short_sale_days(
    fixed: FixedShortSaleReportsL1, *, asof: date, start: date | None = None
) -> tuple[date, ...]:
    """Refresh seven preceding calendar days and recover gaps since retained history.

    An initial run starts at asof minus seven days. Retained normal-empty days
    establish coverage, while failure/held days remain retryable. DiscDate is the
    requested disclosure day, not CalcDate or a margin publication date. Neither
    a market calendar nor L2 row quality enters this plan. An explicit start
    permits finite backfill.
    """
    if start is None:
        start = min(
            [
                asof - timedelta(days=MARKET_REVISION_OVERLAP_DAYS),
                *(date.fromisoformat(day) for day in fixed.value()["scopes"]),
            ]
        )
    return fixed.plan(start=start, end=asof, revision_overlap_days=MARKET_REVISION_OVERLAP_DAYS)


def read_short_sale_requests(
    store: ObjectStore, *, scopes: dict[str, Any], receipts: dict[str, Any], days: Sequence[date]
) -> Iterator[RetainedRequest]:
    return read_full_day_requests(
        store,
        scopes=scopes,
        receipts=receipts,
        days=days,
        contract=_CONTRACT,
        attempt_type=ShortSaleReportsAttempt,
    )
