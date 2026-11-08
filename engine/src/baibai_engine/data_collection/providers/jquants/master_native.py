"""Independent master L1 contract; no L2 or release dependency."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.contracts import (
    RetainedRequest,
)
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

PREFIX = "l1/jquants/master/v1/"
HEAD_KEY = PREFIX + "head.json"
FORMAT = "jquants-master-native-v1"
MAX_PACK_BYTES = 16 * 1024 * 1024
_CONTRACT = FullDayContract("master", PREFIX, FORMAT)
MasterObjectStore = FullDayObjectStore


class MasterAttempt(FullDayAttempt):
    source = "master"


class FixedMasterL1(FixedFullDayL1):
    contract = _CONTRACT
    attempt_type = MasterAttempt


def read_master_requests(
    store: ObjectStore, *, scopes: dict[str, Any], receipts: dict[str, Any], days: Sequence[date]
) -> Iterator[RetainedRequest]:
    return read_full_day_requests(
        store,
        scopes=scopes,
        receipts=receipts,
        days=days,
        contract=_CONTRACT,
        attempt_type=MasterAttempt,
    )


class MasterL1(FullDayL1[MasterAttempt, FixedMasterL1]):
    def __init__(self, store: MasterObjectStore, directory: Path) -> None:
        super().__init__(
            store,
            directory,
            contract=_CONTRACT,
            attempt_type=MasterAttempt,
            fixed_type=FixedMasterL1,
            max_pack_bytes=lambda: MAX_PACK_BYTES,
        )


def plan_master_days(
    store: ObjectStore, fixed: FixedMasterL1, *, start: date, end: date
) -> tuple[date, ...]:
    """Request only explicit missing/unresolved or empty snapshot dates.

    Master collection is sparse, not a calendar-gap backfill. Nonempty is the
    existing acquisition criterion; date/population/field validation belongs to
    the observation consumer and never discards received raw bytes.
    """
    if start > end:
        raise ValueError("invalid master snapshot interval")
    days = tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))
    scopes = fixed.value()["scopes"]
    complete = {
        date.fromisoformat(item.request.params["date"])
        for item in fixed.requests(store, days=days)
        if item.request is not None and item.request.records()
    }
    return tuple(
        day
        for day in days
        if day not in complete
        or scopes.get(day.isoformat(), {}).get("failure")
        or scopes.get(day.isoformat(), {}).get("held")
    )


def verify_migrated_master_receipts(
    connection: sqlite3.Connection, request_ids: Sequence[str], fingerprints: Mapping[str, str]
) -> set[str]:
    """Accept an old writer's bytes only when the finite cutover already retained them."""
    return verify_migrated_full_day_receipts(
        connection, request_ids, fingerprints, attempt_type=MasterAttempt
    )
