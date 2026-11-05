"""EDINET一覧の原受領を、内容アドレス付きgzipと一つのheadに保持する。"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Self, cast

from baibai_engine.data_collection.providers.edinet.models import EdinetResponse
from baibai_engine.data_collection.providers.edinet.receipts import EdinetListReceipt
from baibai_engine.storage.filesystem import write_bytes_atomic
from baibai_engine.storage.json import canonical_json, write_json_atomic
from baibai_engine.storage.objects.r2 import (
    LakeCASConflict,
    LakePublishError,
    ObjectStore,
    RemoteObject,
)

PREFIX = "l1/edinet/lists/v1/"
HEAD_KEY = PREFIX + "head.json"
_FORMAT = "edinet-list-native-v1"

type _BodyCache = dict[str, tuple[bytes, RemoteObject]]


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _body_key(digest: str) -> str:
    return PREFIX + "objects/" + digest + ".body.gz"


@dataclass(frozen=True, slots=True)
class NativeListReceipt:
    receipt_id: str
    doc_date: date
    received_at: datetime
    body_gzip: bytes

    def __post_init__(self) -> None:
        uuid.UUID(self.receipt_id)
        if type(self.doc_date) is not date:
            raise ValueError("EDINET request date must be a date")
        if self.received_at.utcoffset() != timedelta(0):
            raise ValueError("EDINET receipt time must be UTC")
        if not isinstance(self.body_gzip, bytes):
            raise ValueError("EDINET receipt body must be gzip bytes")
        if not self.body_gzip.startswith(b"\x1f\x8b"):
            raise gzip.BadGzipFile("EDINET receipt body is not gzip")
        gzip.decompress(self.body_gzip)

    @classmethod
    def from_response(
        cls, day: date, response: EdinetResponse, receipt_id: str | None = None
    ) -> Self:
        return cls(
            receipt_id if receipt_id is not None else uuid.uuid4().hex,
            day,
            response.received_at,
            gzip.compress(response.body, mtime=0),
        )

    def decoded(self) -> EdinetListReceipt:
        return EdinetListReceipt(
            self.receipt_id, self.doc_date, self.received_at, gzip.decompress(self.body_gzip)
        )

    def _entry(self) -> dict[str, str]:
        return {
            "receipt_id": self.receipt_id,
            "doc_date": self.doc_date.isoformat(),
            "received_at_utc": self.received_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "sha256": _digest(self.body_gzip),
        }


def _head_bytes(value: dict[str, Any] | bytes) -> bytes:
    if isinstance(value, bytes):
        value = json.loads(value)
    if (
        not isinstance(value, dict)
        or set(value) != {"format", "receipts"}
        or value["format"] != _FORMAT
        or not isinstance(value["receipts"], list)
    ):
        raise ValueError("invalid EDINET list native head")
    seen: set[str] = set()
    for entry in value["receipts"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"receipt_id", "doc_date", "received_at_utc", "sha256"}
            or not all(isinstance(item, str) for item in entry.values())
        ):
            raise ValueError("invalid EDINET list receipt metadata")
        uuid.UUID(entry["receipt_id"])
        if entry["receipt_id"] in seen:
            raise ValueError("duplicate EDINET receipt identity")
        seen.add(entry["receipt_id"])
        day = date.fromisoformat(entry["doc_date"])
        instant = datetime.fromisoformat(entry["received_at_utc"])
        if (
            day.isoformat() != entry["doc_date"]
            or instant.utcoffset() != timedelta(0)
            or instant.astimezone(UTC).isoformat(timespec="microseconds")
            != entry["received_at_utc"]
        ):
            raise ValueError("invalid EDINET receipt date or UTC microsecond timestamp")
        digest = entry["sha256"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid EDINET body hash")
    return canonical_json(value)


def _read(
    store: ObjectStore, key: str, *, cache: _BodyCache | None = None
) -> tuple[bytes, str] | None:
    before = store.head(key)
    if before is None:
        if cache is not None:
            cache.pop(key, None)
        return None
    if cache is not None and (cached := cache.get(key)) is not None and cached[1] == before:
        return cached[0], before.etag
    body = store.get_bytes(key)
    after = store.head(key)
    if (
        after is None
        or before != after
        or len(body) != before.size
        or _digest(body) != before.metadata.get("sha256")
    ):
        if cache is not None:
            cache.pop(key, None)
        raise LakePublishError("EDINET list object changed or failed integrity: " + key)
    if cache is not None:
        cache[key] = body, replace(before, metadata=dict(before.metadata))
    return body, before.etag


class FixedEdinetLists:
    """headをコピーして固定する。現行headの更新・削除には依存しない。"""

    def __init__(
        self,
        store: ObjectStore,
        head: dict[str, Any] | bytes,
        *,
        _body_cache: _BodyCache | None = None,
    ) -> None:
        self._store = store
        self._body_cache = _body_cache if _body_cache is not None else {}
        self._payload = _head_bytes(head)

    @property
    def identity(self) -> str:
        return _digest(self._payload)

    def value(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(self._payload))

    def iter_receipts(self, days: Iterable[date] | None = None) -> Iterator[NativeListReceipt]:
        selected = set(days) if days is not None else None
        for entry in self.value()["receipts"]:
            day = date.fromisoformat(entry["doc_date"])
            if selected is not None and day not in selected:
                continue
            saved = _read(self._store, _body_key(entry["sha256"]), cache=self._body_cache)
            if saved is None or _digest(saved[0]) != entry["sha256"]:
                self._body_cache.pop(_body_key(entry["sha256"]), None)
                raise LakePublishError("EDINET list body missing or hash mismatch")
            yield NativeListReceipt(
                entry["receipt_id"],
                day,
                datetime.fromisoformat(entry["received_at_utc"]),
                saved[0],
            )


class EdinetListL1:
    def __init__(self, store: ObjectStore, directory: Path) -> None:
        self.store = store
        self.directory = directory
        self._allow_bootstrap = False
        self._body_cache: _BodyCache = {}

    def _fixed(self, head: dict[str, Any] | bytes) -> FixedEdinetLists:
        return FixedEdinetLists(self.store, head, _body_cache=self._body_cache)

    def require_clean_work(self) -> None:
        if (self.directory / "pending.json").exists():
            raise LakePublishError("EDINET unpublished work exists; call resume_work before HTTP")

    def freeze(self, require_existing: bool = True) -> FixedEdinetLists:
        saved = _read(self.store, HEAD_KEY)
        if saved is None:
            if require_existing:
                raise LakePublishError("EDINET list head is missing; explicit bootstrap required")
            self._allow_bootstrap = True
            return self._fixed({"format": _FORMAT, "receipts": []})
        return self._fixed(saved[0])

    def _put(self, key: str, body: bytes, *, etag: str | None) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / ("head.object" if key == HEAD_KEY else _digest(body) + ".object")
        write_bytes_atomic(path, body)
        try:
            self.store.put_file(
                key,
                path,
                sha256=_digest(body),
                content_md5=base64.b64encode(
                    hashlib.md5(body, usedforsecurity=False).digest()
                ).decode(),
                content_type="application/json" if key == HEAD_KEY else "application/gzip",
                if_match=etag,
                if_none_match=etag is None,
            )
        except LakeCASConflict:
            # A known competing writer is never overwritten or silently rebased.
            raise
        except LakePublishError:
            saved = _read(self.store, key, cache=self._body_cache if key != HEAD_KEY else None)
            if saved is None or saved[0] != body:
                raise
        saved = _read(self.store, key, cache=self._body_cache if key != HEAD_KEY else None)
        if saved is None or saved[0] != body:
            raise LakePublishError("EDINET list write outcome is unconfirmed: " + key)

    def pending_receipts(self) -> tuple[NativeListReceipt, ...]:
        """Read exact unpublished work for the existing private failure handoff."""
        pending = json.loads((self.directory / "pending.json").read_bytes())
        head = self._fixed(pending["head"]).value()
        receipts = []
        for entry in head["receipts"]:
            body = (self.directory / (entry["sha256"] + ".object")).read_bytes()
            if _digest(body) != entry["sha256"]:
                raise LakePublishError("EDINET pending body hash mismatch")
            receipts.append(
                NativeListReceipt(
                    entry["receipt_id"],
                    date.fromisoformat(entry["doc_date"]),
                    datetime.fromisoformat(entry["received_at_utc"]),
                    body,
                )
            )
        return tuple(receipts)

    def resume_work(self) -> FixedEdinetLists:
        """未確定の原受領を再取得せず、新しいheadに条件付きで再公開する。"""
        pending = json.loads((self.directory / "pending.json").read_bytes())
        self._allow_bootstrap = pending["allow_bootstrap"] is True
        return self.publish(self.pending_receipts())

    def publish(self, receipts: Sequence[NativeListReceipt]) -> FixedEdinetLists:
        self.directory.mkdir(parents=True, exist_ok=True)
        pending_path = self.directory / "pending.json"
        # この入力だけを再実行できる作業コピー。正本のhead全体は複製しない。
        input_entries: dict[str, dict[str, str]] = {}
        for receipt in receipts:
            entry = receipt._entry()
            previous = input_entries.get(receipt.receipt_id)
            if previous is not None and previous != entry:
                raise ValueError("EDINET receipt identity conflicts: " + receipt.receipt_id)
            input_entries[receipt.receipt_id] = entry
        pending_head = {
            "format": _FORMAT,
            "receipts": sorted(input_entries.values(), key=lambda e: e["receipt_id"]),
        }
        pending = {"head": pending_head, "allow_bootstrap": self._allow_bootstrap}
        if pending_path.exists():
            existing_work = json.loads(pending_path.read_bytes())
            if existing_work["head"] != pending_head:
                raise LakePublishError("EDINET unpublished work exists; call resume_work first")
        for receipt in receipts:
            write_bytes_atomic(
                self.directory / (_digest(receipt.body_gzip) + ".object"), receipt.body_gzip
            )
        write_json_atomic(pending_path, pending)
        saved = _read(self.store, HEAD_KEY)
        if saved is None:
            if not self._allow_bootstrap:
                raise LakePublishError("EDINET list head is missing; explicit bootstrap required")
            fixed = self._fixed({"format": _FORMAT, "receipts": []})
        else:
            fixed = self._fixed(saved[0])
        entries = {entry["receipt_id"]: entry for entry in fixed.value()["receipts"]}
        # One owner already verified these immutable bytes. Daily source requests
        # must not HEAD/decompress all older bodies again after every receipt.
        unknown_days = {
            date.fromisoformat(entry["doc_date"])
            for entry in entries.values()
            if (cached := self._body_cache.get(_body_key(entry["sha256"]))) is None
            or cached[1].metadata.get("sha256") != entry["sha256"]
        }
        if unknown_days:
            for _ in fixed.iter_receipts(unknown_days):
                pass
        for receipt in receipts:
            entry = receipt._entry()
            existing = entries.get(receipt.receipt_id)
            if existing is not None and existing != entry:
                raise ValueError("EDINET receipt identity conflicts: " + receipt.receipt_id)
            entries[receipt.receipt_id] = entry
        for receipt in receipts:
            key = _body_key(_digest(receipt.body_gzip))
            current = _read(self.store, key, cache=self._body_cache)
            if current is None:
                self._put(key, receipt.body_gzip, etag=None)
            elif current[0] != receipt.body_gzip:
                raise LakePublishError("EDINET immutable body hash mismatch")
        candidate = self._fixed(
            {
                "format": _FORMAT,
                "receipts": sorted(
                    entries.values(),
                    key=lambda e: (e["received_at_utc"], e["doc_date"], e["receipt_id"]),
                ),
            },
        )
        body = canonical_json(candidate.value())
        if saved is None or body != saved[0]:
            self._put(HEAD_KEY, body, etag=saved[1] if saved else None)
        self._allow_bootstrap = False
        pending_path.unlink()
        return candidate
