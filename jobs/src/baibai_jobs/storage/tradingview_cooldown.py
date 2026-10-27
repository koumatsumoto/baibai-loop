"""Carry explicit TradingView Retry-After between Actions runs with object ETag CAS.

This source-local coordination does not acquire the common publication lease.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .lake_publish import Boto3R2Store, LakePublishError, ObjectStore
from .publication_lease import DEFAULT_STORES_BUCKET

KEY = "coordination/tradingview-actions-cooldown.json"


class CooldownError(RuntimeError):
    """The shared waiting deadline could not be trusted or persisted."""


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


def read_deadline(store: ObjectStore) -> tuple[datetime | None, str | None]:
    before = store.head(KEY)
    if before is None:
        return None, None
    if not before.etag or not 0 < before.size <= 4096:
        raise CooldownError("invalid cooldown object")
    try:
        value = json.loads(store.get_bytes(KEY))
    except (ValueError, UnicodeError):
        raise CooldownError("invalid cooldown JSON") from None
    after = store.head(KEY)
    if after is None or before.etag != after.etag:
        raise CooldownError("cooldown changed during read")
    if not isinstance(value, dict) or set(value) != {"not_before_utc"}:
        raise CooldownError("invalid cooldown fields")
    return timestamp(value["not_before_utc"]), after.etag


def extend(store: ObjectStore, deadline: datetime) -> None:
    deadline = timestamp(deadline.isoformat())
    previous, etag = read_deadline(store)
    if previous is not None and previous >= deadline:
        return
    payload = json.dumps({"not_before_utc": deadline.isoformat()}).encode()
    with tempfile.TemporaryDirectory(prefix="baibai-tv-cooldown-") as directory:
        path = Path(directory) / "deadline.json"
        path.write_bytes(payload)
        try:
            store.put_file(
                KEY,
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
            actual, _ = read_deadline(store)
            if actual is None or actual < deadline:
                raise CooldownError("cooldown write incomplete") from None
    actual, _ = read_deadline(store)
    if actual is None or actual < deadline:
        raise CooldownError("cooldown readback mismatch")


def reported_deadline(directory: Path) -> datetime | None:
    lines = ""
    stderr = directory / "tradingview-stderr.log"
    if stderr.exists():
        lines = stderr.read_text(encoding="utf-8")
    result = directory / "tradingview-result.json"
    if result.exists():
        for line in result.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("last_failure"), str):
                lines += "\n" + value["last_failure"]
    deadlines = []
    for line in lines.splitlines():
        if not line.startswith("TradingView acquisition failed; category="):
            continue
        match = re.search(r"; retry_not_before_utc=([^;\s]+)(?:;|$)", line)
        if match:
            deadlines.append(timestamp(match[1]))
    return max(deadlines) if deadlines else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "record"))
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        deadline = reported_deadline(args.directory) if args.command == "record" else None
        if args.command == "record" and deadline is None:
            return 0
        store = Boto3R2Store(bucket=os.environ.get("R2_STORES_BUCKET") or DEFAULT_STORES_BUCKET)
        if args.command == "record":
            assert deadline is not None
            extend(store, deadline)
        else:
            deadline, _ = read_deadline(store)
        allowed = deadline is None or datetime.now(UTC) >= deadline
        values = {
            "allowed": str(allowed).lower(),
            "not_before_utc": deadline.isoformat() if deadline else "",
        }
        if output := os.environ.get("GITHUB_OUTPUT"):
            with Path(output).open("a", encoding="utf-8") as stream:
                for key, value in values.items():
                    print(f"{key}={value}", file=stream)
        print("TradingView cooldown: " + json.dumps(values, sort_keys=True))
        return 0
    except (CooldownError, LakePublishError, OSError, UnicodeError) as exc:
        print(f"TradingView cooldown failed; type={type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
