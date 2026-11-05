"""Independent master L1 contract; no L2 or release dependency."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import zlib
from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsPage,
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


def master_content_id(request: JQuantsRequest) -> str:
    return full_day_content_id(request, _CONTRACT)


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
    migrated = set()
    for identity in request_ids:
        rows = connection.execute(
            "SELECT page_index,source,query_json,received_at_utc,body_gzip "
            "FROM jquants_source_pages WHERE request_id=? ORDER BY page_index",
            (identity,),
        ).fetchall()
        if not any(row[1] == "master" for row in rows):
            continue
        if any(index != row[0] or row[1] != "master" for index, row in enumerate(rows)):
            raise ValueError("legacy master has missing pages or mixed source identity")
        try:
            pages = tuple(
                JQuantsPage(
                    "master",
                    json.loads(query),
                    datetime.fromisoformat(received),
                    gzip.decompress(body),
                )
                for _index, _source, query, received, body in rows
            )
        except (OSError, EOFError, zlib.error) as error:
            raise ValueError("legacy master page cannot be decoded") from error
        query = dict(pages[0].query)
        if set(query) != {"date"}:
            raise ValueError("legacy master does not have an unfiltered explicit date")
        attempt = MasterAttempt(identity, date.fromisoformat(query["date"]), None, pages)
        payload = json.dumps(
            attempt.value(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        if fingerprints.get(identity) != hashlib.sha256(payload).hexdigest():
            raise ValueError("legacy master is absent from or differs from the native cutover")
        migrated.add(identity)
    return migrated
