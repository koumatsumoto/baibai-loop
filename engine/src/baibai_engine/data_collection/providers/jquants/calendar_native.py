"""Range originals and receipt-ordered day selection for native JPX calendars.

Range bodies remain intact. Per-day selection handles overlapping requests;
confirmation clocks never rewrite the timestamps of a retained HTTP page.
"""

from __future__ import annotations

import base64
import gzip
import json
from collections import defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
    JQuantsRequest,
    complete_request,
    page_envelope,
)
from baibai_engine.data_collection.providers.jquants.full_day_native import FullDayObjectStore
from baibai_engine.data_collection.providers.jquants.native_index import (
    IncompleteReceipt,
    NativeMarketIndex,
    ReceiptEvidence,
    plan_calendar,
)
from baibai_engine.data_collection.providers.jquants.native_objects import (
    _digest,
    _json,
    _put,
    _read,
)
from baibai_engine.primitives.jquants_calendar import cash_equity_open
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


def _response_day(raw: object) -> date | None:
    # Unlike from/to, the provider's response has only the dashed date form.
    if not isinstance(raw, str) or len(raw) != 10:
        return None
    try:
        day = date.fromisoformat(raw)
    except ValueError:
        return None
    return day if day.isoformat() == raw else None


def _content(
    request: JQuantsRequest, *, days: Iterable[date] | None = None
) -> tuple[str, dict[str, dict[str, Any]]]:
    start, end = (
        date.fromisoformat(request.params["from"]),
        date.fromisoformat(request.params["to"]),
    )
    rows: list[dict[str, Any]] = []
    extras = []
    for page in request.pages:
        payload = page_envelope(page)
        rows.extend(payload["data"])
        extra = {
            key: value for key, value in payload.items() if key not in {"data", "pagination_key"}
        }
        if extra:
            extras.append(extra)
    grouped: dict[date, list[dict[str, Any]]] = {}
    divisions: dict[date, str] = {}
    valid_range = True
    for row in rows:
        day = _response_day(row.get("Date"))
        if day is None or not start <= day <= end:
            valid_range = False
            continue
        grouped.setdefault(day, []).append(row)
        raw = row.get("HolDiv")
        if raw is not None and (division := str(raw).strip()):
            divisions.setdefault(day, division)
    states = {}
    selected_days = interval(start, end) if days is None else (d for d in days if start <= d <= end)
    for day in selected_days:
        day_rows = grouped.get(day, [])
        states[day.isoformat()] = {
            "content_id": _digest(_json([FORMAT, day.isoformat(), day_rows, extras, valid_range])),
            "valid": valid_range and len(day_rows) == 1 and day in divisions,
            "division": divisions.get(day),
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
        return calendar_index(self.value()["days"])

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


def calendar_index(scopes: Mapping[str, Mapping[str, Any]]) -> NativeMarketIndex:
    """Interpret specified day state for planning or a checked in-memory projection."""
    receipts, failures = [], []
    for raw_day, state in sorted(scopes.items()):
        day = date.fromisoformat(raw_day)
        if (identity := state.get("adopted")) and (confirmed := state.get("confirmed_at")):
            division = state.get("division")
            receipts.append(
                ReceiptEvidence(
                    identity,
                    "market_calendar",
                    day,
                    day,
                    datetime.fromisoformat(confirmed),
                    int(state["valid"]),
                    (day,) if state["valid"] else (),
                    ((day, division),)
                    if isinstance(division, str) and cash_equity_open(division) is not None
                    else (),
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


def _matching_calendar_request_days(
    request: JQuantsRequest, *, scopes: Mapping[str, Mapping[str, Any]]
) -> set[str]:
    """Compare day claims to this already completed request, without fetching its pack again."""
    _, actual = _content(request, days=map(date.fromisoformat, scopes))
    return {
        day
        for day, scope in scopes.items()
        if (content := actual.get(day))
        and content["valid"]
        and scope.get("valid") is True
        and content["content_id"] == scope.get("content_id")
        and content["division"] == scope.get("division")
    }


def validate_calendar_scope_clock(scopes: Mapping[str, Any], evaluated_at: datetime) -> None:
    if evaluated_at.utcoffset() is None:
        raise ValueError("calendar evaluation clock requires a timezone")
    for scope in scopes.values():
        for field in ("confirmed_at", "last_attempt_at", "changed_at"):
            if value := scope.get(field):
                stamp = datetime.fromisoformat(value)
                if stamp.utcoffset() is None or stamp > evaluated_at:
                    raise ValueError(
                        "calendar input contains receipts later than the evaluation clock"
                    )


def verify_calendar_selection(
    store: ObjectStore,
    *,
    scopes: dict[str, Any],
    receipts: dict[str, Any],
    evaluated_at: datetime,
    verify_current_days: bool = True,
) -> dict[str, Any]:
    """Prove day claims in one pack walk; only explicit old replay skips reinterpretation."""
    validate_calendar_scope_clock(scopes, evaluated_at)
    by_adopted: dict[str, dict[str, Any]] = defaultdict(dict)
    for day, scope in scopes.items():
        if identity := scope.get("adopted"):
            by_adopted[identity][day] = scope
    identities = {
        identity
        for scope in scopes.values()
        for field in ("adopted", "failure", "held")
        if (identity := scope.get(field))
    }
    projected = dict(scopes)
    # In either edition transport integrity and dates are checked before SQLite changes.
    # Only a new projection re-evaluates a usable day against today's native rules.
    for attempt in read_calendar_attempts(store, receipts=receipts, identities=identities):
        clocks = [page.received_at for page in attempt.pages]
        if attempt.attempted_at is not None:
            clocks.append(attempt.attempted_at)
        latest_clock = max(clocks)
        if latest_clock > evaluated_at:
            raise ValueError("calendar input contains receipts later than the evaluation clock")
        if (adopted := by_adopted.get(attempt.request_id)) is None:
            continue
        request = complete_request(attempt.pages)
        if any(not attempt.start <= date.fromisoformat(day) <= attempt.end for day in adopted):
            raise ValueError("adopted calendar request differs from its fixed dates")
        claims = {day: scope for day, scope in adopted.items() if scope.get("valid")}
        if verify_current_days and claims:
            matches = _matching_calendar_request_days(request, scopes=claims)
            for day, scope in claims.items():
                confirmed = scope.get("confirmed_at")
                if (
                    day not in matches
                    or not confirmed
                    or datetime.fromisoformat(confirmed) < latest_clock
                ):
                    # Retain the actual selected receipt, hash and clocks. An unproved
                    # claim is neither an open session nor a confirmed closure.
                    projected[day] = {**scope, "valid": False}
    return projected


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
