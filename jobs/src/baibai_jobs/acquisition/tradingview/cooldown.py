"""Carry explicit TradingView Retry-After between Actions runs with object ETag CAS.

This source-local coordination does not acquire the common publication lease.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from baibai_engine.storage.objects.r2 import Boto3R2Store, LakePublishError
from baibai_jobs.acquisition.cooldown import CooldownError, extend, read_deadline, timestamp
from baibai_jobs.publication.lease import DEFAULT_STORES_BUCKET

KEY = "coordination/tradingview-actions-cooldown.json"


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
            extend(store, KEY, deadline)
        else:
            deadline, _ = read_deadline(store, KEY)
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
