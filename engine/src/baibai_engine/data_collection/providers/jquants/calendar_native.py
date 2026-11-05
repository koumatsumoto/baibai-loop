"""Range originals and receipt-ordered day selection for native JPX calendars.

Range bodies remain intact. Per-day selection handles overlapping requests;
confirmation clocks never rewrite the timestamps of a retained HTTP page.
"""

from __future__ import annotations

import base64
import gzip
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
    JQuantsRequest,
    RetainedRequest,
    complete_request,
    page_envelope,
)
from baibai_engine.data_collection.providers.jquants.full_day_native import FullDayObjectStore
from baibai_engine.data_collection.providers.jquants.native_index import (
    IncompleteReceipt,
    NativeMarketIndex,
    ReceiptEvidence,
    index_market_originals,
    plan_calendar,
)
from baibai_engine.data_collection.providers.jquants.native_objects import (
    _digest,
    _json,
    _put,
    _read,
)
from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore

PREFIX = "l1/jquants/market-calendar/v1/"
HEAD_KEY = PREFIX + "head.json"
FORMAT = "jquants-market-calendar-native-v1"
MAX_PACK_BYTES = 16 * 1024 * 1024
CalendarObjectStore = FullDayObjectStore


def interval(start: date, end: date) -> tuple[date, ...]:
    if start > end:
        raise ValueError("calendar range is reversed")
    return tuple(start + timedelta(days=i) for i in range((end - start).days + 1))


@dataclass(frozen=True, slots=True)
class CalendarAttempt:
    request_id: str
    start: date
    end: date
    attempted_at: datetime | None
    pages: tuple[JQuantsPage, ...]
    failure: str | None = None

    def __post_init__(self) -> None:
        interval(self.start, self.end)
        if not self.request_id or (self.attempted_at is None and not self.pages):
            raise ValueError("calendar attempt requires identity and its real clock")
        if self.attempted_at is not None and self.attempted_at.utcoffset() is None:
            raise ValueError("calendar attempt requires a timezone")
        if any(page.source != "market_calendar" for page in self.pages):
            raise ValueError("calendar attempt contains another source")
        if self.pages:
            query = self.pages[0].query
            if set(query) != {"from", "to"} or (
                date.fromisoformat(query["from"]),
                date.fromisoformat(query["to"]),
            ) != (self.start, self.end):
                raise ValueError("calendar attempt requires its full explicit range")

    @property
    def order_time(self) -> datetime:
        if self.pages:
            return self.pages[-1].received_at
        assert self.attempted_at is not None
        return self.attempted_at

    def value(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "attempted_at": self.attempted_at.isoformat() if self.attempted_at else None,
            "failure": self.failure,
            "pages": [
                {
                    "query": dict(p.query),
                    "received_at": p.received_at.isoformat(),
                    "body": base64.b64encode(p.body).decode(),
                }
                for p in self.pages
            ],
        }

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> CalendarAttempt:
        return cls(
            value["request_id"],
            date.fromisoformat(value["start"]),
            date.fromisoformat(value["end"]),
            datetime.fromisoformat(value["attempted_at"]) if value["attempted_at"] else None,
            tuple(
                JQuantsPage(
                    "market_calendar",
                    p["query"],
                    datetime.fromisoformat(p["received_at"]),
                    base64.b64decode(p["body"], validate=True),
                )
                for p in value["pages"]
            ),
            value["failure"],
        )


def _content(request: JQuantsRequest) -> tuple[str, dict[str, dict[str, Any]]]:
    start, end = (
        date.fromisoformat(request.params["from"]),
        date.fromisoformat(request.params["to"]),
    )
    evidence = index_market_originals(
        [RetainedRequest("content", request, None)], source="market_calendar"
    ).receipts[0]
    extras = [
        extra
        for p in request.pages
        if (
            extra := {
                key: val
                for key, val in page_envelope(p).items()
                if key not in {"data", "pagination_key"}
            }
        )
    ]
    rows = request.records()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        try:
            key = date.fromisoformat(str(row.get("Date", ""))[:10]).isoformat()
        except ValueError:
            continue
        grouped.setdefault(key, []).append(row)
    divisions = dict(evidence.calendar_divisions)
    available = set(evidence.calendar_days) - set(evidence.invalid_calendar_days)
    states = {
        day.isoformat(): {
            "content_id": _digest(
                _json(
                    [
                        FORMAT,
                        day.isoformat(),
                        grouped.get(day.isoformat(), []),
                        extras,
                        evidence.valid_calendar,
                    ]
                )
            ),
            "valid": evidence.valid_calendar and day in available,
            "division": divisions.get(day),
        }
        for day in interval(start, end)
    }
    return _digest(_json([FORMAT, start.isoformat(), end.isoformat(), rows, extras])), states


@dataclass(frozen=True, slots=True)
class FixedCalendarL1:
    payload: bytes
    etag: str

    @property
    def content_id(self) -> str:
        return _digest(self.payload)

    def value(self) -> dict[str, Any]:
        value = json.loads(self.payload)
        if not isinstance(value, dict) or value["format"] != FORMAT:
            raise ValueError("unsupported calendar L1 format")
        return value

    def index(self) -> NativeMarketIndex:
        """Derived planning evidence uses confirmations, never retimed raw pages."""
        receipts, failures = [], []
        for raw_day, state in sorted(self.value()["days"].items()):
            day = date.fromisoformat(raw_day)
            if identity := state.get("adopted"):
                division = state.get("division")
                receipts.append(
                    ReceiptEvidence(
                        identity,
                        "market_calendar",
                        day,
                        day,
                        datetime.fromisoformat(state["confirmed_at"]),
                        int(state["valid"]),
                        (day,) if state["valid"] else (),
                        ((day, division),) if division in {"0", "1", "2", "3"} else (),
                        (),
                        bool(state["valid"]),
                    )
                )
            if identity := state.get("failure") or state.get("held"):
                failures.append(
                    IncompleteReceipt(
                        identity,
                        (("from", raw_day), ("to", raw_day)),
                        datetime.fromisoformat(state["last_attempt_at"]),
                    )
                )
        return NativeMarketIndex("market_calendar", tuple(receipts), tuple(failures))

    def plan(self, *, start: date, end: date) -> tuple[date, ...]:
        return plan_calendar(self.index(), start=start, end=end).days

    def requests(
        self, store: ObjectStore, *, days: Sequence[date] | None = None
    ) -> Iterator[CalendarAttempt]:
        value = self.value()
        selected = (
            value["days"]
            if days is None
            else {day.isoformat(): value["days"].get(day.isoformat(), {}) for day in days}
        )
        ids = {
            identity
            for state in selected.values()
            for field in ("adopted", "failure", "held")
            if (identity := state.get(field))
        }
        yield from read_calendar_attempts(store, receipts=value["receipts"], identities=ids)


def read_calendar_attempts(
    store: ObjectStore, *, receipts: dict[str, Any], identities: set[str]
) -> Iterator[CalendarAttempt]:
    packs: dict[str, set[str]] = {}
    for identity in identities:
        packs.setdefault(receipts[identity]["pack"], set()).add(identity)
    for digest, wanted in packs.items():
        saved = _read(store, PREFIX + "packs/" + digest + ".json.gz")
        if saved is None or _digest(saved[0]) != digest:
            raise LakePublishError("fixed calendar pack is unavailable")
        found = set()
        for raw in json.loads(gzip.decompress(saved[0])):
            identity = raw["request_id"]
            if identity not in wanted:
                continue
            if _digest(_json(raw)) != receipts[identity]["fingerprint"]:
                raise LakePublishError("calendar receipt differs from its fixed selection")
            found.add(identity)
            yield CalendarAttempt.from_value(raw)
        if found != wanted:
            raise LakePublishError("calendar pack lacks a selected receipt")


class CalendarL1:
    def __init__(self, store: FullDayObjectStore, directory: Path) -> None:
        self.store, self.directory = store, directory
        directory.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> FixedCalendarL1:
        if next(self.store.iter_objects(PREFIX), None) is not None:
            raise ValueError("calendar prefix is not empty; restore its head")
        _put(
            self.store,
            HEAD_KEY,
            _json({"format": FORMAT, "days": {}, "ranges": {}, "receipts": {}}),
            self.directory,
            etag=None,
        )
        return self.fixed()

    def fixed(self) -> FixedCalendarL1:
        saved = _read(self.store, HEAD_KEY)
        if saved is None:
            raise LakePublishError("calendar L1 requires explicit initialization or cutover")
        fixed = FixedCalendarL1(*saved)
        fixed.value()
        return fixed

    def commit(self, attempts: Sequence[CalendarAttempt]) -> dict[str, list[str]]:
        fixed = self.fixed()
        value = fixed.value()
        report: dict[str, list[str]] = {
            key: [] for key in ("changed", "equal", "failed", "held", "retried")
        }
        packed = []
        for item in attempts:
            raw, identity = item.value(), item.request_id
            fingerprint = _digest(_json(raw))
            if prior := value["receipts"].get(identity):
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("calendar request identity conflicts")
                report["retried"].append(identity)
                continue
            try:
                request = complete_request(item.pages)
            except JQuantsAcquisitionError:
                request = None
            stamp = item.order_time
            range_key = item.start.isoformat() + "/" + item.end.isoformat()
            content, states = _content(request) if request else (None, {})
            days = [day.isoformat() for day in interval(item.start, item.end)]
            newer = [
                day
                for day in days
                if not (at := value["days"].get(day, {}).get("last_attempt_at"))
                or stamp > datetime.fromisoformat(at)
            ]
            equal = bool(
                request
                and all(
                    day in newer
                    or (
                        not (scope := value["days"].get(day, {})).get("held")
                        and not scope.get("failure")
                        and stamp == datetime.fromisoformat(scope["last_attempt_at"])
                    )
                    for day in days
                )
                and value["ranges"].get(range_key, {}).get("content_id") == content
                and all(
                    value["days"].get(day, {}).get("content_id") == states[day]["content_id"]
                    for day in days
                )
            )
            kind = (
                "equal"
                if equal
                else "held"
                if not newer
                else "failed"
                if request is None
                else "changed"
            )
            report[kind].append(identity)
            value["receipts"][identity] = {"fingerprint": fingerprint}
            if not equal:
                packed.append(raw)
                value["receipts"][identity]["pack"] = ""
            for day in days:
                scope = dict(value["days"].get(day, {}))
                prior_at = scope.get("last_attempt_at")
                if prior_at and stamp <= datetime.fromisoformat(prior_at):
                    if stamp == datetime.fromisoformat(prior_at) and not equal:
                        value["days"][day] = {**scope, "held": identity}
                    continue
                scope["last_attempt_at"] = stamp.isoformat()
                if request is None:
                    scope["failure"] = identity
                else:
                    changed = scope.get("content_id") != states[day]["content_id"]
                    scope.update(
                        states[day],
                        adopted=scope["adopted"] if equal else identity,
                        confirmed_at=stamp.isoformat(),
                        confirmation_id=identity,
                        failure=None,
                        held=None,
                    )
                    if changed:
                        scope["changed_at"] = stamp.isoformat()
                value["days"][day] = scope
            if request is not None:
                prior_range = value["ranges"].get(range_key, {})
                if not prior_range or stamp > datetime.fromisoformat(prior_range["received_at"]):
                    value["ranges"][range_key] = {
                        "content_id": content,
                        "received_at": stamp.isoformat(),
                    }
        if value == fixed.value():
            return report
        # Calendar responses are small. Keep each actual range intact and bound packs.
        batches: list[list[dict[str, Any]]] = []
        for raw in packed:
            if len(_json([raw])) > MAX_PACK_BYTES:
                raise ValueError("single calendar range exceeds pack limit; keep acquisition work")
            if not batches or len(_json([*batches[-1], raw])) > MAX_PACK_BYTES:
                batches.append([])
            batches[-1].append(raw)
        for batch in batches:
            body = gzip.compress(_json(batch), mtime=0)
            digest = _digest(body)
            for raw in batch:
                value["receipts"][raw["request_id"]]["pack"] = digest
            _put(
                self.store, PREFIX + "packs/" + digest + ".json.gz", body, self.directory, etag=None
            )
        _put(self.store, HEAD_KEY, _json(value), self.directory, etag=fixed.etag)
        return report


def verify_migrated_calendar_receipts(
    connection: sqlite3.Connection, request_ids: Sequence[str], fingerprints: Mapping[str, str]
) -> set[str]:
    """Only finite-cutover byte proof permits removing an old range from replay."""
    migrated = set()
    for identity in request_ids:
        rows = connection.execute(
            "SELECT page_index,source,query_json,received_at_utc,body_gzip "
            "FROM jquants_source_pages WHERE request_id=? ORDER BY page_index",
            (identity,),
        ).fetchall()
        if not any(row[1] == "market_calendar" for row in rows):
            continue
        if any(row[0] != i or row[1] != "market_calendar" for i, row in enumerate(rows)):
            raise ValueError("old calendar has missing pages or mixed source identity")
        pages = tuple(
            JQuantsPage(
                "market_calendar",
                json.loads(query),
                datetime.fromisoformat(at),
                gzip.decompress(body),
            )
            for _index, _source, query, at, body in rows
        )
        _verify_migrated_calendar_pages(identity, pages, fingerprints)
        migrated.add(identity)
    return migrated


def _verify_migrated_calendar_pages(
    identity: str, pages: tuple[JQuantsPage, ...], fingerprints: Mapping[str, str]
) -> None:
    query = pages[0].query
    if set(query) != {"from", "to"}:
        raise ValueError("old calendar requires a full explicit range")
    attempt = CalendarAttempt(
        identity, date.fromisoformat(query["from"]), date.fromisoformat(query["to"]), None, pages
    )
    if fingerprints.get(identity) != _digest(_json(attempt.value())):
        raise ValueError("old calendar is absent from or differs from the finite cutover")


def verify_migrated_calendar_dependency(
    value: dict[str, Any], fingerprints: Mapping[str, str]
) -> None:
    from baibai_engine.data_collection.providers.jquants.dependencies import read_dependency

    request = read_dependency(value)
    if request.source != "market_calendar":
        raise ValueError("unexpected migrated calendar dependency source")
    _verify_migrated_calendar_pages(value["request_id"], request.pages, fingerprints)
