"""財務の日別完了requestを独立確定するL1。L2・release・SQLite sealを読まない。

headだけが公開済み集合の正本。本文は不変のbyte上限付きpackへまとめる。
同値確認はheadの取得状態だけを更新し、原本の受領時刻を書き換えない。
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from baibai_engine.data_collection.providers.jquants.contracts import (
    JQuantsAcquisitionError,
    JQuantsPage,
    JQuantsRequest,
    RetainedRequest,
    complete_request,
    page_envelope,
)
from baibai_engine.storage.objects.r2 import LakeCASConflict, LakePublishError, ObjectStore

PREFIX = "l1/jquants/financial-summaries/v1/"
HEAD_KEY = PREFIX + "head.json"
FORMAT = "jquants-financial-native-v1"
MAX_PACK_BYTES = 16 * 1024 * 1024


def _json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _day(request: JQuantsRequest) -> str:
    if request.source != "financial_summaries" or set(request.params) != {"date"}:
        raise ValueError("financial native L1 requires an unfiltered explicit source day")
    return date.fromisoformat(request.params["date"]).isoformat()


def financial_content_id(request: JQuantsRequest) -> str:
    """Ignore only pagination mechanics; preserve unknown fields and record order.

    Row reordering is conservatively a change. Page boundaries without extra
    envelope fields do not change content; non-pagination envelope fields remain.
    Exact page bytes, queries and timestamps are retained for changed requests.
    """
    extras = [
        extra
        for page in request.pages
        if (
            extra := {
                k: v for k, v in page_envelope(page).items() if k not in {"data", "pagination_key"}
            }
        )
    ]
    return _digest(_json([FORMAT, _day(request), request.records(), extras]))


@dataclass(frozen=True, slots=True)
class FinancialAttempt:
    request_id: str
    day: date
    attempted_at: datetime | None
    pages: tuple[JQuantsPage, ...]
    failure: str | None = None

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("attempt identity is required")
        if self.attempted_at is None:
            if not self.pages:
                raise ValueError("an attempt without pages requires its actual attempt time")
        elif self.attempted_at.utcoffset() is None:
            raise ValueError("attempt time must be timezone-aware")
        for page in self.pages:
            if page.source != "financial_summaries":
                raise ValueError("unexpected source in financial attempt")

    @property
    def order_time(self) -> datetime:
        """Use real page receipt time; a migrated start time may be unknown."""
        if self.pages:
            return self.pages[-1].received_at
        if self.attempted_at is None:
            raise ValueError("attempt time is unavailable")
        return self.attempted_at

    def value(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "day": self.day.isoformat(),
            "attempted_at": self.attempted_at.isoformat() if self.attempted_at else None,
            "failure": self.failure,
            "pages": [
                {
                    "query": dict(page.query),
                    "received_at": page.received_at.isoformat(),
                    "body": base64.b64encode(page.body).decode(),
                }
                for page in self.pages
            ],
        }

    @classmethod
    def from_value(cls, value: dict[str, Any]) -> FinancialAttempt:
        return cls(
            value["request_id"],
            date.fromisoformat(value["day"]),
            datetime.fromisoformat(value["attempted_at"])
            if value["attempted_at"] is not None
            else None,
            tuple(
                JQuantsPage(
                    "financial_summaries",
                    item["query"],
                    datetime.fromisoformat(item["received_at"]),
                    base64.b64decode(item["body"], validate=True),
                )
                for item in value["pages"]
            ),
            value["failure"],
        )


def _read(store: ObjectStore, key: str) -> tuple[bytes, str] | None:
    before = store.head(key)
    if before is None:
        return None
    payload = store.get_bytes(key)
    after = store.head(key)
    if (
        after is None
        or before.etag != after.etag
        or len(payload) != before.size
        or _digest(payload) != before.metadata.get("sha256")
    ):
        raise LakePublishError("financial L1 object changed or failed integrity")
    return payload, before.etag


def _put(
    store: ObjectStore, key: str, payload: bytes, directory: Path, *, etag: str | None
) -> None:
    """Retry the identical conditional write, never a newly generated receipt."""
    digest = _digest(payload)
    path = directory / (digest + ".object")
    path.write_bytes(payload)
    for attempt in range(2):
        try:
            store.put_file(
                key,
                path,
                sha256=digest,
                content_md5=base64.b64encode(
                    hashlib.md5(payload, usedforsecurity=False).digest()
                ).decode(),
                content_type="application/octet-stream",
                if_match=etag,
                if_none_match=etag is None,
            )
        except LakePublishError:
            saved = _read(store, key)
            if saved is not None and saved[0] == payload:
                return
            if saved is not None and saved[1] != etag:
                raise LakeCASConflict("financial L1 writer changed; retry from its head") from None
            if attempt:
                raise
        else:
            saved = _read(store, key)
            if saved is None or saved[0] != payload:
                raise LakePublishError("financial L1 write outcome is unconfirmed")
            return


@dataclass(frozen=True, slots=True)
class FixedFinancialL1:
    """Exact head bytes also fix confirmation and unresolved failure state."""

    payload: bytes
    etag: str

    @property
    def content_id(self) -> str:
        return _digest(self.payload)

    def value(self) -> dict[str, Any]:
        value = json.loads(self.payload)
        if not isinstance(value, dict):
            raise ValueError("financial L1 head must be an object")
        return value

    def plan(self, *, start: date, end: date, revision_overlap_days: int) -> tuple[date, ...]:
        if start > end or revision_overlap_days < 0:
            raise ValueError("invalid financial acquisition interval")
        value = self.value()
        if value["pending"] is not None:
            raise LakePublishError("resolve the pending financial L1 commit before acquisition")
        scopes = value["scopes"]
        recent = end - timedelta(days=revision_overlap_days)
        return tuple(
            day
            for offset in range((end - start).days + 1)
            if (day := start + timedelta(days=offset)) >= recent
            or not (scope := scopes.get(day.isoformat()))
            or not scope.get("adopted")
            or scope.get("failure")
            or scope.get("held")
        )

    def requests(self, store: ObjectStore, *, days: Sequence[date]) -> Iterator[RetainedRequest]:
        """Read current adopted originals and relevant failures from this fixed head."""
        value = self.value()
        wanted: dict[str, dict[str, Any]] = {}
        for day in days:
            scope = value["scopes"].get(day.isoformat(), {})
            for field in ("adopted", "failure"):
                identity = scope.get(field)
                if identity:
                    wanted[identity] = value["receipts"][identity]
        by_pack: dict[str, set[str]] = {}
        for identity, receipt in wanted.items():
            by_pack.setdefault(receipt["pack"], set()).add(identity)
        for digest, identities in by_pack.items():
            saved = _read(store, PREFIX + "packs/" + digest + ".json.gz")
            if saved is None or _digest(saved[0]) != digest:
                raise LakePublishError("fixed financial L1 pack is unavailable")
            values = json.loads(gzip.decompress(saved[0]))
            found: set[str] = set()
            for raw in values:
                identity = raw["request_id"]
                if identity not in identities:
                    continue
                if _digest(_json(raw)) != wanted[identity]["fingerprint"]:
                    raise LakePublishError("financial receipt differs from its fixed head")
                found.add(identity)
                item = FinancialAttempt.from_value(raw)
                try:
                    request = complete_request(item.pages)
                except JQuantsAcquisitionError:
                    request = None
                yield RetainedRequest(
                    identity,
                    request,
                    None if request else item.failure or "incomplete request",
                    {"date": item.day.isoformat()},
                    item.order_time,
                )
            if found != identities:
                raise LakePublishError("financial pack lacks a fixed receipt")
        for day in days:
            scope = value["scopes"].get(day.isoformat(), {})
            if identity := scope.get("held"):
                yield RetainedRequest(
                    identity,
                    None,
                    "ambiguous receipt order",
                    {"date": day.isoformat()},
                    datetime.fromisoformat(scope["last_attempt_at"]),
                )
        # An in-flight commit is not published content. Consumers retain the
        # known incomplete ranges instead of reporting the prior head as fresh.
        if value["pending"] is not None:
            for day in days:
                if day.isoformat() in value["pending"]["days"]:
                    yield RetainedRequest(
                        "pending:" + day.isoformat(),
                        None,
                        "unconfirmed L1 commit",
                        {"date": day.isoformat()},
                    )


class FinancialObjectStore(ObjectStore, Protocol):
    def iter_objects(self, prefix: str) -> Iterator[tuple[str, int]]: ...


class FinancialL1:
    def __init__(self, store: FinancialObjectStore, directory: Path) -> None:
        self.store, self.directory = store, directory
        directory.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> FixedFinancialL1:
        """Explicit empty initialization, never an implicit missing-head fallback."""
        if next(self.store.iter_objects(PREFIX), None) is not None:
            raise ValueError("financial L1 prefix is not empty; restore its head instead")
        _put(
            self.store,
            HEAD_KEY,
            _json({"format": FORMAT, "scopes": {}, "receipts": {}, "pending": None}),
            self.directory,
            etag=None,
        )
        return self.fixed()

    def fixed(self) -> FixedFinancialL1:
        saved = _read(self.store, HEAD_KEY)
        if saved is None:
            raise LakePublishError("financial L1 is not initialized; explicit cutover is required")
        value = json.loads(saved[0])
        if value["format"] != FORMAT:
            raise ValueError("unsupported financial L1 format")
        return FixedFinancialL1(*saved)

    def resume(self) -> FixedFinancialL1:
        """Resolve pending PUTs before comparing or acquiring another request."""
        fixed = self.fixed()
        pending = fixed.value()["pending"]
        if pending is None:
            return fixed
        for digest in pending["packs"]:
            key = PREFIX + "packs/" + digest + ".json.gz"
            saved = _read(self.store, key)
            if saved is None:
                path = self.directory / (digest + ".object")
                if not path.is_file() or _digest(path.read_bytes()) != digest:
                    raise LakePublishError(
                        "pending financial pack requires its saved work directory"
                    )
                _put(self.store, key, path.read_bytes(), self.directory, etag=None)
            elif _digest(saved[0]) != digest:
                raise LakePublishError("pending financial pack conflicts")
        _put(self.store, HEAD_KEY, _json(pending["next"]), self.directory, etag=fixed.etag)
        return self.fixed()

    def commit(self, attempts: Sequence[FinancialAttempt]) -> dict[str, list[str]]:
        fixed = self.resume()
        value = fixed.value()
        report: dict[str, list[str]] = {
            key: [] for key in ("changed", "equal", "failed", "held", "retried")
        }
        packed: list[dict[str, Any]] = []
        affected: set[str] = set()
        for item in attempts:
            raw = item.value()
            identity, day = item.request_id, item.day.isoformat()
            fingerprint = _digest(_json(raw))
            scope = value["scopes"].get(day, {})
            prior = value["receipts"].get(identity)
            if prior is None and scope.get("confirmation_id") == identity:
                prior = {"fingerprint": scope["confirmation_fingerprint"]}
            if prior is not None:
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("financial request identity conflicts")
                report["retried"].append(identity)
                continue
            try:
                request = complete_request(item.pages)
                if _day(request) != day:
                    raise ValueError("financial request differs from its attempted day")
            except JQuantsAcquisitionError:
                request = None
            stamp = item.order_time
            previous_at = scope.get("last_attempt_at")
            held = previous_at is not None and stamp <= datetime.fromisoformat(previous_at)
            content_id = financial_content_id(request) if request else None
            equal = request is not None and content_id == scope.get("content_id")
            if held:
                kind = "held"
            elif request is None:
                kind = "failed"
            elif equal:
                kind = "equal"
            else:
                kind = "changed"
            report[kind].append(identity)
            # Keep compact identities even for older equal confirmations, so
            # replaying a saved acquisition directory never stores equal bodies.
            value["receipts"][identity] = {"fingerprint": fingerprint}
            if kind != "equal":
                packed.append(raw)
                value["receipts"][identity]["pack"] = ""
            if held:
                if stamp == datetime.fromisoformat(previous_at):
                    affected.add(day)
                    value["scopes"][day] = {**scope, "held": identity}
                continue
            affected.add(day)
            scope = {**scope, "last_attempt_at": stamp.isoformat()}
            if request is None:
                scope["failure"] = identity
            else:
                scope.update(
                    {
                        "adopted": scope["adopted"] if equal else identity,
                        "content_id": content_id,
                        "confirmed_at": stamp.isoformat(),
                        "confirmation_id": identity,
                        "confirmation_fingerprint": fingerprint,
                        "failure": None,
                        "held": None,
                    }
                )
                if not equal:
                    scope["changed_at"] = stamp.isoformat()
            value["scopes"][day] = scope
        packs = self._packs(packed, value)
        if value == fixed.value():
            return report
        # Stage the exact candidate and immutable identities before the first
        # payload PUT. Runner loss cannot turn an ambiguous PUT into a new fetch.
        pending_head = fixed.value()
        pending_head["pending"] = {"next": value, "packs": packs, "days": sorted(affected)}
        _put(self.store, HEAD_KEY, _json(pending_head), self.directory, etag=fixed.etag)
        self.resume()
        return report

    def _packs(self, rows: list[dict[str, Any]], head: dict[str, Any]) -> list[str]:
        packs: list[str] = []
        batch: list[dict[str, Any]] = []
        size = 2

        def flush() -> None:
            if not batch:
                return
            body = gzip.compress(_json(batch), mtime=0)
            digest = _digest(body)
            (self.directory / (digest + ".object")).write_bytes(body)
            packs.append(digest)
            for row in batch:
                head["receipts"][row["request_id"]]["pack"] = digest

        for row in rows:
            count = len(_json(row)) + 1
            if count + 2 > MAX_PACK_BYTES:
                raise ValueError(
                    "single financial request exceeds pack limit; retained work needs review"
                )
            if size + count > MAX_PACK_BYTES:
                flush()
                batch, size = [], 2
            batch.append(row)
            size += count
        flush()
        return packs
