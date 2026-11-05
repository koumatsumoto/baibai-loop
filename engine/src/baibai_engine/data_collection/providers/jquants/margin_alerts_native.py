"""Independent publication-day originals for the designated-issue margin subset."""

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

PREFIX = "l1/jquants/margin-alerts/v1/"
HEAD_KEY = PREFIX + "head.json"
FORMAT = "jquants-margin-alerts-native-v1"
_CONTRACT = FullDayContract("margin_alerts", PREFIX, FORMAT, query_date_format="%Y-%m-%d")


class MarginAlertsAttempt(FullDayAttempt):
    source = "margin_alerts"


class FixedMarginAlertsL1(FixedFullDayL1):
    contract = _CONTRACT
    attempt_type = MarginAlertsAttempt


class MarginAlertsL1(FullDayL1[MarginAlertsAttempt, FixedMarginAlertsL1]):
    def __init__(self, store: FullDayObjectStore, directory: Path) -> None:
        super().__init__(
            store,
            directory,
            contract=_CONTRACT,
            attempt_type=MarginAlertsAttempt,
            fixed_type=FixedMarginAlertsL1,
            max_pack_bytes=lambda: 16 * 1024 * 1024,
        )


def plan_margin_alert_days(
    fixed: FixedMarginAlertsL1, *, asof: date, start: date | None = None
) -> tuple[date, ...]:
    """Refresh seven preceding calendar days and recover gaps since retained history.

    An initial run starts at asof minus seven days. Retained normal-empty days
    establish coverage, while failure/held days remain retryable. No all-issues
    population, balance-date lag, market calendar or observation quality enters
    this source's publication-date plan. An explicit start permits finite backfill.
    """
    if start is None:
        start = min(
            [
                asof - timedelta(days=MARKET_REVISION_OVERLAP_DAYS),
                *(date.fromisoformat(day) for day in fixed.value()["scopes"]),
            ]
        )
    return fixed.plan(start=start, end=asof, revision_overlap_days=MARKET_REVISION_OVERLAP_DAYS)


def read_margin_alert_requests(
    store: ObjectStore, *, scopes: dict[str, Any], receipts: dict[str, Any], days: Sequence[date]
) -> Iterator[RetainedRequest]:
    return read_full_day_requests(
        store,
        scopes=scopes,
        receipts=receipts,
        days=days,
        contract=_CONTRACT,
        attempt_type=MarginAlertsAttempt,
    )
