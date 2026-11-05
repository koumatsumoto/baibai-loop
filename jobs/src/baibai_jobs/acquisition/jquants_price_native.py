"""Acquire price originals without a prior L2 store or release."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, date, datetime

from baibai_engine.data_collection.providers.jquants.http import JQuantsHTTP
from baibai_engine.data_collection.providers.jquants.price_native import (
    PriceAttempt,
    PriceL1,
)
from baibai_engine.storage.objects.r2 import Boto3R2Store
from baibai_jobs.acquisition.jquants_full_day_native import acquire_full_day_native, native_main


def acquire_price_native(
    owner: PriceL1,
    *,
    start: date,
    end: date,
    revision_overlap_days: int,
    client: JQuantsHTTP,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    return acquire_full_day_native(
        owner,
        start=start,
        end=end,
        revision_overlap_days=revision_overlap_days,
        client=client,
        clock=clock,
        sleep=sleep,
    )


def main(argv: list[str] | None = None) -> int:
    return native_main(
        argv,
        description=__doc__ or "",
        owner_type=PriceL1,
        attempt_type=PriceAttempt,
        store_factory=Boto3R2Store,
        client_factory=JQuantsHTTP,
    )


if __name__ == "__main__":
    raise SystemExit(main())
