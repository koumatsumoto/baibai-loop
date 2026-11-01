"""Persist source/quota Retry-After independently of work cycles and projections.

Callers own the quota key. A later deadline can extend it, never shorten it.
The saved deadline is coordination only, not evidence of a successful receipt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from baibai_engine.storage.objects.r2 import LakePublishError, ObjectStore


class CooldownError(RuntimeError):
    """The shared waiting deadline could not be trusted or persisted."""


def quota_key(quota: str) -> str:
    if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", quota) is None:
        raise ValueError("invalid acquisition quota")
    return f"coordination/source-cooldowns/{quota}.json"


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise CooldownError("invalid deadline")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise CooldownError("invalid deadline") from None
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise CooldownError("deadline must be UTC")
    return moment.astimezone(UTC)


def read_deadline(store: ObjectStore, key: str) -> tuple[datetime | None, str | None]:
    before = store.head(key)
    if before is None:
        return None, None
    if not before.etag or not 0 < before.size <= 4096:
        raise CooldownError("invalid cooldown object")
    try:
        value = json.loads(store.get_bytes(key))
    except (ValueError, UnicodeError):
        raise CooldownError("invalid cooldown JSON") from None
    after = store.head(key)
    if after is None or before.etag != after.etag:
        raise CooldownError("cooldown changed during read")
    if not isinstance(value, dict) or set(value) != {"not_before_utc"}:
        raise CooldownError("invalid cooldown fields")
    return timestamp(value["not_before_utc"]), after.etag


def extend(store: ObjectStore, key: str, deadline: datetime) -> None:
    deadline = timestamp(deadline.isoformat())
    previous, etag = read_deadline(store, key)
    if previous is not None and previous >= deadline:
        return
    payload = json.dumps({"not_before_utc": deadline.isoformat()}).encode()
    with tempfile.TemporaryDirectory(prefix="baibai-source-cooldown-") as directory:
        path = Path(directory) / "deadline.json"
        path.write_bytes(payload)
        try:
            store.put_file(
                key,
                path,
                sha256=hashlib.sha256(payload).hexdigest(),
                content_md5=base64.b64encode(
                    hashlib.md5(payload, usedforsecurity=False).digest()
                ).decode(),
                content_type="application/json",
                if_match=etag,
                if_none_match=etag is None,
            )
        except LakePublishError:
            # A lost response or a concurrent extension must never shorten the wait.
            actual, _ = read_deadline(store, key)
            if actual is None or actual < deadline:
                raise CooldownError("cooldown write incomplete") from None
    actual, _ = read_deadline(store, key)
    if actual is None or actual < deadline:
        raise CooldownError("cooldown readback mismatch")
